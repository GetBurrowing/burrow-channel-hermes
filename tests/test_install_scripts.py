"""install.sh / uninstall.sh / update.sh against a temp HERMES_HOME, with the
stub `hermes` in tests/stub_hermes on PATH.

The scripts under test are the WORKING-TREE copies: each test builds a fresh
git checkout (``checkout`` fixture) from the repository's tracked files plus
the current scripts, commits it, and runs the scripts from there, so the
installer's "run from a checkout" path installs a known commit.

The token is a random sentinel per test. The no-argv test reads the stub's
argv log (every `hermes` child) and, where strace exists, every execve of the
whole process tree. ``test_argv_check_fires_on_a_token_leaking_installer``
keeps the check honest: it runs an installer mutated to pass the token on
argv and requires the same check to catch it.
"""

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
STUB_DIR = Path(__file__).resolve().parent / "stub_hermes"
SCRIPTS = ("install.sh", "uninstall.sh", "update.sh")

INSTALLED = "hermes-suite-channel: installed"
UNINSTALLED = "hermes-suite-channel: uninstalled"
UPDATED = "hermes-suite-channel: updated"

PRE_CONFIG = "# user config, hand written\nmodel:\n  default: qwen3.5-9b  # trailing comment\n"
PRE_ENV = 'OPENAI_API_KEY=dummy-not-a-key\nexport FOO="bar baz"\n# a comment line\n'
TOKEN_REL = "mcp-tokens/startup-suite-platform.runtime-token"


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _make_checkout(dest: Path, install_sh: str | None = None) -> Path:
    dest.mkdir(parents=True)
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout.split("\0")
    for rel in [*filter(None, tracked), *SCRIPTS, "tests/stub_hermes/hermes"]:
        src = ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    if install_sh is not None:
        (dest / "install.sh").write_text(install_sh)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "test snapshot")
    return dest


@pytest.fixture
def checkout(tmp_path) -> Path:
    return _make_checkout(tmp_path / "checkout")


@pytest.fixture
def token() -> str:
    return "SENTINEL-01a0d6c5-" + secrets.token_hex(16)


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "hermes-home"
    h.mkdir()
    (h / "config.yaml").write_text(PRE_CONFIG)
    (h / ".env").write_text(PRE_ENV)
    (h / "SOUL.md").write_text("persona\n")
    return h


@pytest.fixture
def env(tmp_path, home) -> dict:
    return {
        **{
            k: v
            for k, v in os.environ.items()
            if k not in ("HERMES_SUITE_CHANNEL_REPO", "HERMES_BURROW_CHANNEL_REPO")
        },
        # The stub runs on `python3`: this interpreter, which has ruamel.yaml.
        "PATH": os.pathsep.join(
            [str(STUB_DIR), str(Path(sys.executable).parent), os.environ["PATH"]]
        ),
        "HERMES_HOME": str(home),
        "HERMES_STUB_LOG": str(tmp_path / "stub-argv.jsonl"),
    }


def run(script: Path, *args: str, env: dict, stdin: str | None = None, **kw):
    return subprocess.run(
        ["bash", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        start_new_session=True,  # no controlling terminal: /dev/tty cannot be opened
        timeout=120,
        **kw,
    )


def install(checkout, home, env, token, *extra, **kw):
    return run(
        checkout / "install.sh",
        "--url",
        "wss://suite.example.test/runtime/ws",
        "--runtime-id",
        "rt-01a0d6c5",
        "--hermes-home",
        str(home),
        *extra,
        env=env,
        stdin=token,
        **kw,
    )


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def stub_calls(env) -> list[list[str]]:
    path = Path(env["HERMES_STUB_LOG"])
    if not path.exists():
        return []
    return [json.loads(line)["argv"][1:] for line in path.read_text().splitlines()]


def last_line(proc) -> str:
    lines = proc.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def token_path(home: Path) -> Path:
    return home / TOKEN_REL


def set_lines(proc) -> list[str]:
    """The `Set <key>` lines `hermes config set` prints (stub and real CLI)."""
    return [ln for ln in proc.stderr.splitlines() if "Set " in ln]


# ------------------------------------------------------------------ install


def test_install_writes_env_plugin_and_mcp(checkout, home, env, token):
    proc = install(checkout, home, env, token)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == INSTALLED

    env_text = (home / ".env").read_text()
    assert env_text.startswith(PRE_ENV)
    # Both spellings: the plugin reads BURROW_ first, v0.2.0 reads SUITE_ only.
    assert env_text[len(PRE_ENV) :] == (
        "BURROW_URL=wss://suite.example.test/runtime/ws\n"
        "SUITE_URL=wss://suite.example.test/runtime/ws\n"
        "BURROW_RUNTIME_ID=rt-01a0d6c5\n"
        "SUITE_RUNTIME_ID=rt-01a0d6c5\n"
        f"BURROW_RUNTIME_TOKEN_FILE={token_path(home)}\n"
        f"SUITE_RUNTIME_TOKEN_FILE={token_path(home)}\n"
    )
    assert token not in env_text
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    assert token_path(home).read_text() == token + "\n"
    assert stat.S_IMODE(token_path(home).stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path(home).parent.stat().st_mode) == 0o700

    plugin = home / "plugins" / "startup-suite-platform"
    assert (plugin / "plugin.yaml").is_file()
    sha = _git(checkout, "rev-parse", "HEAD")
    assert (plugin / "hermes_suite_channel" / "_build.py").read_text().count(
        f'BUILD = "{sha}"'
    ) == 1

    cfg_text = (home / "config.yaml").read_text()
    assert cfg_text.startswith(PRE_CONFIG)
    assert token not in cfg_text
    cfg = config(home)
    assert cfg["mcp_servers"]["startup-suite"] == {
        "command": sys.executable,
        "args": [
            str(plugin / "hermes_suite_channel" / "mcp_bridge.py"),
            "--url",
            "https://suite.example.test/mcp",
            "--token-file",
            str(token_path(home)),
        ],
    }
    assert cfg["plugins"]["enabled"] == ["startup-suite-platform"]

    calls = stub_calls(env)
    assert ["plugins", "enable", "startup-suite-platform"] in calls
    assert [c[:2] for c in calls if c[:2] == ["plugins", "install"]] == [["plugins", "install"]]


def test_hermes_children_get_no_stdin(checkout, home, env, token):
    install(checkout, home, env, token)
    records = [json.loads(x) for x in Path(env["HERMES_STUB_LOG"]).read_text().splitlines()]
    assert records
    assert not any(r["stdin_isatty"] for r in records)


def test_second_install_changes_nothing(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    before = snapshot(home)
    n_calls = len(stub_calls(env))

    again = install(checkout, home, env, token)
    assert again.returncode == 0, again.stderr
    assert last_line(again) == INSTALLED
    assert snapshot(home) == before
    assert set_lines(again) == []
    mutating = [
        c
        for c in stub_calls(env)[n_calls:]
        if c[:2]
        in (["plugins", "install"], ["plugins", "enable"], ["config", "set"], ["config", "unset"])
    ]
    assert mutating == []


def _raw_gets(env) -> list[list[str]]:
    """`hermes config get` calls that could return an expanded value: any
    `--raw`, and any read of the MCP entry."""
    return [
        c
        for c in stub_calls(env)
        if c[:2] == ["config", "get"] and ("--raw" in c or c[2].startswith("mcp_servers"))
    ]


def test_config_is_never_read_back_through_hermes(checkout, home, env, token):
    """`hermes config get --raw` EXPANDS ${VAR} from the environment, so an
    installer comparing against it pulls expanded values through a child's
    stdout into shell. install.sh compares the raw YAML instead."""
    for _ in range(2):
        assert install(checkout, home, env, token).returncode == 0
    assert run(checkout / "update.sh", "--hermes-home", str(home), env=env).returncode == 0
    assert run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env).returncode == 0
    assert stub_calls(env), "the stub recorded no calls, so the check cannot fire"
    assert _raw_gets(env) == []


def test_raw_get_check_fires_on_an_installer_that_reads_back(tmp_path, home, env, token):
    """Positive control for the check above."""
    src = (ROOT / "install.sh").read_text()
    anchor = '  local key="$1" value="$2" rc=0\n'
    assert anchor in src
    leaky = src.replace(
        anchor, anchor + '  run_hermes config get "$key" --raw >/dev/null || true\n'
    )
    co = _make_checkout(tmp_path / "leaky", install_sh=leaky)
    assert install(co, home, env, token).returncode == 0
    assert _raw_gets(env) != []


def test_round_trip_restores_every_preexisting_byte(checkout, home, env, token):
    before = snapshot(home)
    assert install(checkout, home, env, token).returncode == 0
    assert install(checkout, home, env, token).returncode == 0

    un = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env)
    assert un.returncode == 0, un.stderr
    assert last_line(un) == UNINSTALLED

    after = snapshot(home)
    assert {k: after.get(k) for k in before} == before
    assert sha(home / "config.yaml") == hashlib.sha256(PRE_CONFIG.encode()).hexdigest()
    assert not (home / "plugins" / "startup-suite-platform").exists()
    assert set(after) - set(before) == set()
    assert not (home / "mcp-tokens").exists()
    assert not (home / ".suite-channel").exists()

    again = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env)
    assert again.returncode == 0 and last_line(again) == UNINSTALLED
    assert snapshot(home) == after


def test_uninstall_keeps_an_env_file_without_a_final_newline(checkout, home, env, token):
    (home / ".env").write_text("KEEP=1")  # no trailing newline
    assert install(checkout, home, env, token).returncode == 0
    assert run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env).returncode == 0
    # The installer's append had to terminate the last line; that newline is
    # the one byte a round trip cannot give back without keeping state.
    assert (home / ".env").read_text() == "KEEP=1\n"


# -------------------------------------------------------------------- token


@pytest.mark.parametrize("form", ["separate", "equals"])
def test_token_flag_is_refused(checkout, home, env, token, form):
    before = snapshot(home)
    args = ["--token", token] if form == "separate" else [f"--token={token}"]
    proc = install(checkout, home, env, "", *args)
    assert proc.returncode != 0
    assert "--token is refused" in proc.stderr
    assert "ps" in proc.stderr and "--token-file" in proc.stderr
    assert token not in proc.stdout + proc.stderr
    assert snapshot(home) == before
    assert stub_calls(env) == []


def test_token_from_token_file(checkout, home, env, token, tmp_path):
    tf = tmp_path / "tok"
    tf.write_text(token + "\n")
    proc = install(checkout, home, env, "", "--token-file", str(tf))
    assert proc.returncode == 0, proc.stderr
    assert token_path(home).read_text() == token + "\n"
    assert token not in (home / ".env").read_text()


def test_piped_script_does_not_read_the_token_from_stdin(checkout, home, env, token, tmp_path):
    """`curl ... | bash -s -- ...`: stdin is the script itself. With no
    --token-file and no terminal the installer must stop, not read script
    bytes as a token."""
    script = (checkout / "install.sh").read_text()
    base = ["bash", "-s", "--", "--url", "wss://h.test/runtime/ws", "--runtime-id", "rt"]
    kw = dict(capture_output=True, text=True, env=env, start_new_session=True, timeout=60)
    proc = subprocess.run([*base, "--hermes-home", str(home)], input=script, **kw)
    assert proc.returncode != 0
    assert "no token" in proc.stderr
    assert "SUITE_RUNTIME_TOKEN" not in (home / ".env").read_text()
    assert not token_path(home).exists()

    tf = tmp_path / "tok"
    tf.write_text(token)
    ok = subprocess.run(
        [*base, "--hermes-home", str(home), "--token-file", str(tf), "--repo", str(checkout)],
        input=script,
        **kw,
    )
    assert ok.returncode == 0, ok.stderr
    assert token_path(home).read_text() == token + "\n"


# ------------------------------------------------- install source repository

# A raw literal, not read from the scripts, so a default that drifts in either
# script fails here rather than being echoed back.
DEFAULT_REPO_URL = "https://github.com/Startup-Suite/burrow-channel-hermes.git"


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_default_repo_names_burrow_channel_hermes(script):
    lines = [
        ln
        for ln in (ROOT / script).read_text().splitlines()
        if ln.startswith("readonly DEFAULT_REPO=")
    ]
    assert lines == [
        'readonly DEFAULT_REPO="${HERMES_BURROW_CHANNEL_REPO:-${HERMES_SUITE_CHANNEL_REPO:-'
        + DEFAULT_REPO_URL
        + '}}"'
    ]
    assert "hermes-suite-channel.git" not in (ROOT / script).read_text()


def _piped(script_path: Path, args: list[str], env: dict, stdin_extra: str = ""):
    """`curl ... | bash -s -- ...`: no checkout beside the script, so the
    script clones its source repository."""
    return subprocess.run(
        ["bash", "-s", "--", *args],
        input=script_path.read_text(),
        capture_output=True,
        text=True,
        env=env,
        start_new_session=True,
        timeout=120,
    )


def _piped_install(checkout, home, env, tmp_path, token, *extra):
    tf = tmp_path / "tok"
    tf.write_text(token)
    args = ["--url", "wss://h.test/runtime/ws", "--runtime-id", "rt"]
    args += ["--hermes-home", str(home), "--token-file", str(tf), *extra]
    return _piped(checkout / "install.sh", args, env)


def test_repo_env_var_reaches_the_clone(checkout, home, env, token, tmp_path):
    proc = _piped_install(
        checkout, home, {**env, "HERMES_SUITE_CHANNEL_REPO": str(checkout)}, tmp_path, token
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cloning {checkout}\n" in proc.stderr
    assert DEFAULT_REPO_URL not in proc.stderr
    head = _git(checkout, "rev-parse", "HEAD")
    plugin = home / "plugins" / "startup-suite-platform"
    assert f'BUILD = "{head}"' in (plugin / "hermes_suite_channel" / "_build.py").read_text()


@pytest.mark.parametrize("form", ["--repo", "--repo="])
def test_repo_flag_beats_the_env_var(checkout, home, env, token, tmp_path, form):
    bogus = tmp_path / "no-such-repo"
    flag = [f"--repo={checkout}"] if form == "--repo=" else ["--repo", str(checkout)]
    proc = _piped_install(
        checkout, home, {**env, "HERMES_SUITE_CHANNEL_REPO": str(bogus)}, tmp_path, token, *flag
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cloning {checkout}\n" in proc.stderr
    assert str(bogus) not in proc.stderr


def test_repo_default_is_unchanged_without_flag_or_env(checkout, home, env, token, tmp_path):
    env = {
        k: v
        for k, v in env.items()
        if k not in ("HERMES_SUITE_CHANNEL_REPO", "HERMES_BURROW_CHANNEL_REPO")
    }
    # Never touch the network: refuse every transport but file://.
    env["GIT_ALLOW_PROTOCOL"] = "file"
    proc = _piped_install(checkout, home, env, tmp_path, token)
    assert f"cloning {DEFAULT_REPO_URL}\n" in proc.stderr
    assert proc.returncode != 0
    assert f"could not clone {DEFAULT_REPO_URL}" in proc.stderr
    assert "Startup-Suite" not in proc.stderr.split("could not clone", 1)[1].replace(
        DEFAULT_REPO_URL, ""
    )


def test_update_repo_default_without_flag_or_env(checkout, home, env, token, tmp_path):
    assert install(checkout, home, env, token).returncode == 0
    env = {
        k: v
        for k, v in env.items()
        if k not in ("HERMES_SUITE_CHANNEL_REPO", "HERMES_BURROW_CHANNEL_REPO")
    }
    # Never touch the network: refuse every transport but file://.
    env["GIT_ALLOW_PROTOCOL"] = "file"
    proc = _piped(checkout / "update.sh", ["--hermes-home", str(home)], env)
    assert proc.returncode != 0
    assert f"could not clone {DEFAULT_REPO_URL}" in proc.stderr


def test_update_burrow_repo_env_var_beats_the_suite_one(checkout, home, env, token, tmp_path):
    assert install(checkout, home, env, token).returncode == 0
    bogus = tmp_path / "no-such-repo"
    proc = _piped(
        checkout / "update.sh",
        ["--hermes-home", str(home)],
        {
            **env,
            "HERMES_BURROW_CHANNEL_REPO": str(checkout),
            "HERMES_SUITE_CHANNEL_REPO": str(bogus),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    assert str(bogus) not in proc.stderr
    assert "deprecated" not in proc.stderr


def test_update_resolves_the_env_var_and_passes_it_on(checkout, home, env, token, tmp_path):
    assert install(checkout, home, env, token).returncode == 0
    (checkout / "NEWFILE").write_text("x\n")
    _git(checkout, "add", "NEWFILE")
    _git(checkout, "commit", "-q", "-m", "next")
    new_sha = _git(checkout, "rev-parse", "HEAD")

    proc = _piped(
        checkout / "update.sh",
        ["--hermes-home", str(home)],
        {**env, "HERMES_SUITE_CHANNEL_REPO": str(checkout)},
    )
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    plugin = home / "plugins" / "startup-suite-platform"
    assert f'BUILD = "{new_sha}"' in (plugin / "hermes_suite_channel" / "_build.py").read_text()


def test_update_repo_flag_beats_the_env_var(checkout, home, env, token, tmp_path):
    assert install(checkout, home, env, token).returncode == 0
    bogus = tmp_path / "no-such-repo"
    proc = _piped(
        checkout / "update.sh",
        ["--hermes-home", str(home), "--repo", str(checkout)],
        {**env, "HERMES_SUITE_CHANNEL_REPO": str(bogus)},
    )
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    assert str(bogus) not in proc.stderr


def test_burrow_repo_env_var_reaches_the_clone_without_a_warning(
    checkout, home, env, token, tmp_path
):
    proc = _piped_install(
        checkout, home, {**env, "HERMES_BURROW_CHANNEL_REPO": str(checkout)}, tmp_path, token
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cloning {checkout}\n" in proc.stderr
    assert "deprecated" not in proc.stderr


def test_suite_repo_env_var_still_works_and_warns_once(checkout, home, env, token, tmp_path):
    proc = _piped_install(
        checkout, home, {**env, "HERMES_SUITE_CHANNEL_REPO": str(checkout)}, tmp_path, token
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cloning {checkout}\n" in proc.stderr
    assert proc.stderr.count("HERMES_SUITE_CHANNEL_REPO is deprecated") == 1


def test_burrow_repo_env_var_beats_the_suite_one(checkout, home, env, token, tmp_path):
    bogus = tmp_path / "no-such-repo"
    both = {
        **env,
        "HERMES_BURROW_CHANNEL_REPO": str(checkout),
        "HERMES_SUITE_CHANNEL_REPO": str(bogus),
    }
    proc = _piped_install(checkout, home, both, tmp_path, token)
    assert proc.returncode == 0, proc.stderr
    assert f"cloning {checkout}\n" in proc.stderr
    assert str(bogus) not in proc.stderr
    assert "deprecated" not in proc.stderr

    (checkout / "NEWFILE").write_text("x\n")
    _git(checkout, "add", "NEWFILE")
    _git(checkout, "commit", "-q", "-m", "next")
    up = _piped(checkout / "update.sh", ["--hermes-home", str(home)], both)
    assert up.returncode == 0, up.stderr
    assert last_line(up) == UPDATED
    assert str(bogus) not in up.stderr


def test_update_warns_once_for_the_suite_repo_env_var(checkout, home, env, token, tmp_path):
    assert install(checkout, home, env, token).returncode == 0
    proc = _piped(
        checkout / "update.sh",
        ["--hermes-home", str(home)],
        {**env, "HERMES_SUITE_CHANNEL_REPO": str(checkout)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("HERMES_SUITE_CHANNEL_REPO is deprecated") == 1


def test_empty_token_is_refused(checkout, home, env):
    proc = install(checkout, home, env, "")
    assert proc.returncode != 0 and "empty" in proc.stderr


def _argv_leaks(env, token) -> list[list[str]]:
    calls = stub_calls(env)
    assert calls, "the stub recorded no calls, so the argv check cannot fire"
    return [c for c in calls if any(token in a for a in c)]


def test_token_never_on_a_hermes_child_argv(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    assert run(checkout / "update.sh", "--hermes-home", str(home), env=env).returncode == 0
    assert run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env).returncode == 0
    assert _argv_leaks(env, token) == []


def test_argv_check_fires_on_a_token_leaking_installer(tmp_path, home, env, token):
    """Positive control: the same check must go red for an installer that
    hands the token to `hermes config set` on argv."""
    src = (ROOT / "install.sh").read_text()
    anchor = "  store_token\n"
    assert anchor in src
    leaking = src.replace(anchor, anchor + '  hermes config set SUITE_RUNTIME_TOKEN "$TOKEN" >&2\n')
    co = _make_checkout(tmp_path / "leaky", install_sh=leaking)
    assert install(co, home, env, token).returncode == 0
    assert _argv_leaks(env, token) != []


@pytest.mark.skipif(shutil.which("strace") is None, reason="strace not installed")
def test_token_on_no_execve_argv_at_all(checkout, home, env, token, tmp_path):
    """Every process the installer starts, not only `hermes`: git, sed, mv, ..."""
    trace = tmp_path / "execve.trace"
    proc = subprocess.run(
        [
            "strace",
            "-f",
            "-qq",
            "-e",
            "trace=execve",
            "-s",
            "4096",
            "-o",
            str(trace),
            "bash",
            str(checkout / "install.sh"),
            "--url",
            "wss://h.test/runtime/ws",
            "--runtime-id",
            "rt",
            "--hermes-home",
            str(home),
        ],
        input=token,
        capture_output=True,
        text=True,
        env=env,
        start_new_session=True,
        timeout=120,
    )
    if proc.returncode != 0 and "ptrace" in proc.stderr:
        pytest.skip(f"strace cannot trace here: {proc.stderr.strip()[:200]}")
    assert proc.returncode == 0, proc.stderr
    lines = trace.read_text().splitlines()
    execs = [ln for ln in lines if "execve(" in ln]
    assert any("stub_hermes/hermes" in ln for ln in execs), "trace saw no hermes child"
    assert any('"git"' in ln or '/git"' in ln for ln in execs), "trace saw no git child"
    assert [ln for ln in execs if token in ln] == []


# ----------------------------------------------------------------- fallback


def test_copy_fallback_is_opt_in(checkout, home, env, token):
    env = {**env, "HERMES_STUB_FAIL_INSTALL": "1"}
    refused = install(checkout, home, env, token)
    assert refused.returncode != 0
    assert "--copy-fallback" in refused.stderr
    assert not (home / "plugins" / "startup-suite-platform").exists()

    ok = install(checkout, home, env, token, "--copy-fallback")
    assert ok.returncode == 0, ok.stderr
    assert last_line(ok) == INSTALLED
    sha = _git(checkout, "rev-parse", "HEAD")
    build = home / "plugins" / "startup-suite-platform" / "hermes_suite_channel" / "_build.py"
    assert f'BUILD = "{sha}"' in build.read_text()

    un = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env)
    assert un.returncode == 0, un.stderr
    assert not (home / "plugins" / "startup-suite-platform").exists()


# --------------------------------------------------------------------- scan


def _install_calls(env):
    return [c for c in stub_calls(env) if c[:2] == ["plugins", "install"]]


def test_caution_scan_is_accepted_with_force_and_said_so(checkout, home, env, token):
    env = {**env, "HERMES_STUB_SCAN": "caution"}
    proc = install(checkout, home, env, token)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == INSTALLED
    assert "returned CAUTION" in proc.stderr and "--refuse-scan-caution" in proc.stderr
    calls = _install_calls(env)
    assert ["--force" in c for c in calls] == [False, True]


def test_caution_scan_refused_on_request(checkout, home, env, token):
    env = {**env, "HERMES_STUB_SCAN": "caution"}
    proc = install(checkout, home, env, token, "--refuse-scan-caution")
    assert proc.returncode != 0
    assert not any("--force" in c for c in _install_calls(env))
    assert not (home / "plugins" / "startup-suite-platform").exists()


def test_dangerous_scan_is_never_forced(checkout, home, env, token):
    env = {**env, "HERMES_STUB_SCAN": "dangerous"}
    proc = install(checkout, home, env, token)
    assert proc.returncode != 0
    calls = _install_calls(env)
    assert calls and not any("--force" in c for c in calls)


# ------------------------------------------------------------------- update


def test_update_moves_the_plugin_and_keeps_the_config(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    env_before = (home / ".env").read_bytes()
    cfg_before = (home / "config.yaml").read_bytes()

    (checkout / "NEWFILE").write_text("x\n")
    _git(checkout, "add", "NEWFILE")
    _git(checkout, "commit", "-q", "-m", "next")
    new_sha = _git(checkout, "rev-parse", "HEAD")

    proc = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    plugin = home / "plugins" / "startup-suite-platform"
    assert (plugin / "NEWFILE").is_file()
    assert f'BUILD = "{new_sha}"' in (plugin / "hermes_suite_channel" / "_build.py").read_text()
    assert (home / ".env").read_bytes() == env_before
    assert (home / "config.yaml").read_bytes() == cfg_before

    again = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
    assert again.returncode == 0 and last_line(again) == UPDATED


def test_update_refuses_when_not_installed(checkout, home, env):
    proc = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode != 0 and "run install.sh first" in proc.stderr


# ------------------------------------- reinstall over an ENABLED plugin
#
# hermes-agent fdec926e refuses to replace an enabled plugin that declares
# Python dependencies unless a terminal user consents (plugins_transaction.py
# publish_plugin(require_consent=True)), and every Hermes child install.sh
# runs has stdin on /dev/null: "Reinstall declined: dependency install
# skipped (non-interactive)". The stub reproduces that refusal; install.sh
# disables, installs, and enables again.

# The block that does the disable, cut out to build the installer the
# positive controls run. Keyed on the two lines that open and close it.
DISABLE_OPEN = '    if [ -e "$(plugin_dir)" ] && plugin_enabled; then\n'
DISABLE_CLOSE = "      REENABLE_ON_EXIT=1\n    fi\n"


def without_disable_step(text: str) -> str:
    start = text.index(DISABLE_OPEN)
    end = text.index(DISABLE_CLOSE, start) + len(DISABLE_CLOSE)
    out = text[:start] + text[end:]
    assert "run_hermes plugins disable" not in out
    return out


def _commit_next(checkout: Path, name: str = "NEWFILE") -> str:
    (checkout / name).write_text("x\n")
    _git(checkout, "add", name)
    _git(checkout, "commit", "-q", "-m", "next")
    return _git(checkout, "rev-parse", "HEAD")


def _plugin_verbs(env) -> list[str]:
    return [c[1] for c in stub_calls(env) if c[:1] == ["plugins"]]


def _build(home: Path) -> str:
    f = home / "plugins" / "startup-suite-platform" / "hermes_suite_channel" / "_build.py"
    return f.read_text().split('BUILD = "')[1].split('"')[0]


def _enabled(home: Path) -> list[str]:
    return list((config(home).get("plugins") or {}).get("enabled") or [])


def test_update_over_an_enabled_plugin_disables_installs_enables(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    cfg_before = (home / "config.yaml").read_bytes()
    Path(env["HERMES_STUB_LOG"]).unlink()
    new_sha = _commit_next(checkout)

    proc = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    assert _plugin_verbs(env) == ["disable", "install", "enable"]
    installs = _install_calls(env)
    assert len(installs) == 1 and "--force" in installs[0] and "--no-enable" in installs[0]
    assert _build(home) == new_sha
    assert _enabled(home) == ["startup-suite-platform"]
    # disable + enable give config.yaml back byte for byte (real CLI measured
    # the same; the stub mirrors it).
    assert (home / "config.yaml").read_bytes() == cfg_before


def test_update_without_the_disable_step_is_declined(tmp_path, home, env, token):
    """Positive control: the installer before this fix (the disable block cut
    out) fails on the same host with Hermes's refusal and leaves the old
    build, so the test above is measuring the disable step."""
    co = _make_checkout(
        tmp_path / "control", install_sh=without_disable_step((ROOT / "install.sh").read_text())
    )
    assert install(co, home, env, token).returncode == 0
    old_sha = _build(home)
    _commit_next(co)
    proc = run(co / "update.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode != 0
    assert "Reinstall declined: dependency install skipped (non-interactive)" in proc.stderr
    assert _build(home) == old_sha


def test_failed_reinstall_re_enables_the_old_version(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    old_sha = _build(home)
    cfg_before = (home / "config.yaml").read_bytes()
    Path(env["HERMES_STUB_LOG"]).unlink()
    _commit_next(checkout)

    proc = run(
        checkout / "update.sh",
        "--hermes-home",
        str(home),
        env={**env, "HERMES_STUB_FAIL_INSTALL": "1"},
    )
    assert proc.returncode != 0
    assert last_line(proc) != UPDATED
    verbs = _plugin_verbs(env)
    assert verbs[0] == "disable" and verbs[-1] == "enable" and "install" in verbs
    assert "re-enabled" in proc.stderr
    assert _build(home) == old_sha
    assert not (home / "plugins" / "startup-suite-platform" / "NEWFILE").exists()
    assert _enabled(home) == ["startup-suite-platform"]
    assert (home / "config.yaml").read_bytes() == cfg_before


def test_re_enable_check_fires_without_the_trap(tmp_path, home, env, token):
    """Mutation: with the exit-trap re-enable removed, the same failure leaves
    the plugin disabled, which the test above would catch."""
    text = (ROOT / "install.sh").read_text()
    mutated = text.replace("    reenable_after_failure || true\n", "    true\n")
    assert mutated != text
    co = _make_checkout(tmp_path / "mutant", install_sh=mutated)
    assert install(co, home, env, token).returncode == 0
    _commit_next(co)
    proc = run(
        co / "update.sh", "--hermes-home", str(home), env={**env, "HERMES_STUB_FAIL_INSTALL": "1"}
    )
    assert proc.returncode != 0
    assert _enabled(home) == []


def test_unknown_ref_fails_before_any_disable(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    Path(env["HERMES_STUB_LOG"]).unlink()
    proc = run(
        checkout / "update.sh",
        "--hermes-home",
        str(home),
        "--ref",
        "0123456789abcdef0123456789abcdef01234567",
        env=env,
    )
    assert proc.returncode != 0 and "no such commit" in proc.stderr
    assert "disable" not in _plugin_verbs(env)
    assert _enabled(home) == ["startup-suite-platform"]


def test_update_of_a_disabled_plugin_does_not_disable_first(checkout, home, env, token):
    """The path for a plugin that is installed but not enabled is unchanged:
    install --force, then enable."""
    assert install(checkout, home, env, token).returncode == 0
    subprocess.run(["hermes", "plugins", "disable", "startup-suite-platform"], env=env, check=True)
    Path(env["HERMES_STUB_LOG"]).unlink()
    new_sha = _commit_next(checkout)
    proc = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode == 0, proc.stderr
    assert _plugin_verbs(env) == ["install", "enable"]
    assert _build(home) == new_sha and _enabled(home) == ["startup-suite-platform"]


# ----------------------------------------------------- config.yaml round trip


def _uninstall(checkout, home, env):
    proc = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UNINSTALLED
    return proc


def test_bookkeeping_keys_hermes_adds_are_removed(checkout, home, env, token):
    """The stub mirrors the real CLI: after `plugins remove` config.yaml holds
    `plugins: {enabled: [], disabled: []}` and `_config_version: 46`. The
    uninstall must take both away when they were not there before."""
    pre = sha(home / "config.yaml")
    assert install(checkout, home, env, token).returncode == 0
    un = _uninstall(checkout, home, env)
    assert sha(home / "config.yaml") == pre
    assert "restored byte for byte" in un.stderr


def test_bookkeeping_check_fires_without_the_cleanup(tmp_path, home, env, token):
    """Positive control: an uninstaller with step 3 removed leaves the
    Hermes-written lines, and the sha comparison sees it."""
    src = (ROOT / "uninstall.sh").read_text()
    start = src.index('if [ -f "$RECORD_DIR/record" ]; then\n  "$HPY" -')
    end = src.index("PYEOF\nfi\n", start) + len("PYEOF\nfi\n")
    co = _make_checkout(tmp_path / "nocleanup")
    (co / "uninstall.sh").write_text(src[:start] + src[end:])
    pre = sha(home / "config.yaml")
    assert install(co, home, env, token).returncode == 0
    _uninstall(co, home, env)
    assert sha(home / "config.yaml") != pre
    assert "_config_version" in (home / "config.yaml").read_text()


def test_an_edited_config_keeps_the_edit_and_loses_only_the_bookkeeping(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    with (home / "config.yaml").open("a") as f:
        f.write("user_added: true  # edited after install\n")
    un = _uninstall(checkout, home, env)
    text = (home / "config.yaml").read_text()
    assert text.startswith(PRE_CONFIG)
    assert "user_added: true  # edited after install" in text
    assert "_config_version" not in text and "plugins" not in text
    assert "was edited after the install" in un.stderr
    assert config(home) == {"model": {"default": "qwen3.5-9b"}, "user_added": True}


def test_a_config_that_did_not_exist_is_removed_again(checkout, home, env, token):
    (home / "config.yaml").unlink()
    assert install(checkout, home, env, token).returncode == 0
    assert (home / "config.yaml").exists()
    _uninstall(checkout, home, env)
    assert not (home / "config.yaml").exists()


def test_a_preexisting_plugins_block_survives_byte_for_byte(checkout, home, env, token):
    original = PRE_CONFIG + "plugins:\n  enabled:\n    - some-other-plugin\n"
    (home / "config.yaml").write_text(original)
    assert install(checkout, home, env, token).returncode == 0
    assert config(home)["plugins"]["enabled"] == ["some-other-plugin", "startup-suite-platform"]
    _uninstall(checkout, home, env)
    assert (home / "config.yaml").read_text() == original


def test_install_over_a_legacy_install_migrates_the_token_and_mcp_entry(checkout, home, env, token):
    """A stage-4 install: the token in .env, an http MCP entry interpolating
    ${SUITE_RUNTIME_TOKEN}. Reinstalling moves both to the token-file form."""
    (home / ".env").write_text(PRE_ENV + f"SUITE_RUNTIME_TOKEN={token}\n")
    (home / "config.yaml").write_text(
        PRE_CONFIG
        + "mcp_servers:\n  startup-suite:\n    url: https://suite.example.test/mcp\n"
        + "    headers:\n      Authorization: Bearer ${SUITE_RUNTIME_TOKEN}\n"
    )
    proc = install(checkout, home, env, token)
    assert proc.returncode == 0, proc.stderr
    assert "SUITE_RUNTIME_TOKEN=" not in (home / ".env").read_text()
    entry = config(home)["mcp_servers"]["startup-suite"]
    assert set(entry) == {"command", "args"}
    assert "SUITE_RUNTIME_TOKEN" not in (home / "config.yaml").read_text()
    assert (home / ".suite-channel" / "record").read_text().startswith("pre_install_known=0\n")
    _uninstall(checkout, home, env)
    assert "startup-suite" not in (home / "config.yaml").read_text()


def test_find_hermes_python_is_the_same_code_in_both_scripts():
    def body(name, fn):
        src = (ROOT / name).read_text()
        start = src.index(f"{fn}() {{")
        return src[start : src.index("\n}\n", start)]

    for fn in ("find_hermes_python", "hermes_venv_python"):
        assert body("install.sh", fn) == body("uninstall.sh", fn), fn


# ------------------------------------------- a #!/bin/sh launcher (fdec926e)
#
# Every launcher hermes-agent fdec926e installs is a POSIX sh wrapper
# (`#!/bin/sh` + `exec <python> -I -c ... "$@"`), measured in a throwaway
# HERMES_HOME. The stub above has a python shebang, which is why an installer
# that took the shebang for the interpreter passed here and failed on the real
# thing with "/bin/sh is older than Python 3.8". These run the scripts through
# a real sh wrapper.


def _sh_launcher(tmp_path: Path) -> Path:
    d = tmp_path / "launcher-bin"
    d.mkdir()
    launcher = d / "hermes"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB_DIR / "hermes"}" "$@"\n')
    launcher.chmod(0o755)
    return launcher


def _venv(tmp_path: Path) -> Path:
    """A venv-shaped dir whose bin/python is this interpreter (it has ruamel.yaml)."""
    venv = tmp_path / "hermes-venv"
    (venv / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    (venv / "bin").mkdir()
    py = venv / "bin" / "python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    return venv


def _sh_env(env: dict, launcher: Path, **extra: str) -> dict:
    # The launcher's directory first, and NO python3 ahead of the system one
    # that could stand in for a missing answer: the fallback is tested apart.
    return {**env, "PATH": os.pathsep.join([str(launcher.parent), os.environ["PATH"]]), **extra}


def _mcp_command(home: Path) -> str:
    return config(home)["mcp_servers"]["startup-suite"]["command"]


def test_a_sh_launcher_resolves_to_the_venv_hermes_selects(checkout, home, env, token, tmp_path):
    launcher = _sh_launcher(tmp_path)
    venv = _venv(tmp_path)
    proc = install(
        checkout,
        home,
        _sh_env(env, launcher, HERMES_STUB_VENV=str(venv)),
        token,
        "--hermes",
        str(launcher),
    )
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == INSTALLED
    assert "older than Python" not in proc.stderr
    assert _mcp_command(home) == str(venv / "bin" / "python")
    # Asked of Hermes itself, through the wrapper.
    assert ["--run-module", "pm.environments"] in stub_calls(env)


def test_the_sh_launcher_check_fires_on_the_old_shebang_rule(tmp_path, home, env, token):
    """Positive control: an installer without the shell-shebang guard takes
    /bin/sh for python and fails exactly as the pinned one did."""
    src = (ROOT / "install.sh").read_text()
    guard = '    case "${interp##*/}" in\n      python*) ;;\n      *) interp="" ;;\n    esac\n'
    assert guard in src
    mutated = _make_checkout(tmp_path / "mutated", install_sh=src.replace(guard, ""))
    launcher = _sh_launcher(tmp_path)
    venv = _venv(tmp_path)
    proc = install(
        mutated,
        home,
        _sh_env(env, launcher, HERMES_STUB_VENV=str(venv)),
        token,
        "--hermes",
        str(launcher),
    )
    assert proc.returncode != 0
    assert "/bin/sh is older than Python 3.8" in proc.stderr


def test_python_flag_wins_over_every_guess(checkout, home, env, token, tmp_path):
    launcher = _sh_launcher(tmp_path)
    venv = _venv(tmp_path)
    explicit = _venv(tmp_path / "explicit") / "bin" / "python"
    proc = install(
        checkout,
        home,
        _sh_env(env, launcher, HERMES_STUB_VENV=str(venv)),
        token,
        "--hermes",
        str(launcher),
        "--python",
        str(explicit),
    )
    assert proc.returncode == 0, proc.stderr
    assert _mcp_command(home) == str(explicit)
    assert ["--run-module", "pm.environments"] not in stub_calls(env)


def test_python_flag_that_is_not_executable_is_refused(checkout, home, env, token, tmp_path):
    proc = install(checkout, home, env, token, "--python", str(tmp_path / "no-such-python"))
    assert proc.returncode != 0
    assert "--python" in proc.stderr and "not an executable file" in proc.stderr


def test_a_sh_launcher_without_an_answer_falls_back_to_python3_not_sh(
    checkout, home, env, token, tmp_path
):
    launcher = _sh_launcher(tmp_path)
    proc = install(checkout, home, _sh_env(env, launcher), token, "--hermes", str(launcher))
    assert proc.returncode == 0, proc.stderr
    assert Path(_mcp_command(home)).name.startswith("python")
    assert _mcp_command(home) != "/bin/sh"


def test_uninstall_through_a_sh_launcher(checkout, home, env, token, tmp_path):
    launcher = _sh_launcher(tmp_path)
    venv = _venv(tmp_path)
    e = _sh_env(env, launcher, HERMES_STUB_VENV=str(venv))
    assert install(checkout, home, e, token, "--hermes", str(launcher)).returncode == 0
    un = run(
        checkout / "uninstall.sh", "--hermes-home", str(home), "--hermes", str(launcher), env=e
    )
    assert un.returncode == 0, un.stderr
    assert "startup-suite" not in (home / "config.yaml").read_text()


# --------------------------------------------------------- the REAL hermes
#
# HERMES_REAL_BIN: a real `hermes` (hermes-agent fdec926e in the recorded
# runs). A pm-installed Hermes keeps its dependency environment under its
# ROOT home and refuses a HERMES_HOME outside it ("dependency environment is
# missing or outside this install"), and a bare directory under the root is
# not a live profile, so Hermes's consent gate never sees its plugins. Each
# test therefore gets its own LIVE profile (`hermes profile create`), deleted
# afterwards. That writes into the Hermes root, so it also needs
# HERMES_REAL_ROOT_IS_DISPOSABLE=1: never point these at a Hermes you use.

REAL_HERMES = os.environ.get("HERMES_REAL_BIN")
REAL_OK = bool(REAL_HERMES) and os.environ.get("HERMES_REAL_ROOT_IS_DISPOSABLE") == "1"
real = pytest.mark.skipif(
    not REAL_OK,
    reason="needs HERMES_REAL_BIN (a real hermes) and HERMES_REAL_ROOT_IS_DISPOSABLE=1",
)
# install.sh at dd199a01, the tip the behavioural gate failed on, when this
# clone has that commit.
PRE_FIX = "dd199a01aa5d3dd1ef49957c83e4d5c12752f233"


def _real_env(home: Path | None = None) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HERMES_HOME", "HERMES_SUITE_CHANNEL_REPO", "HERMES_BURROW_CHANNEL_REPO")
    }
    env["PATH"] = f"{Path(REAL_HERMES).parent}{os.pathsep}{os.environ['PATH']}"
    if home is not None:
        env["HERMES_HOME"] = str(home)
    return env


def _real_hermes(*args: str, home: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [REAL_HERMES, *args],
        env=_real_env(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture
def real_home():
    # ONE real test per Hermes root. Measured at fdec926e: after one profile
    # enabled this plugin and was removed and deleted, a second profile's
    # `hermes plugins enable` fails ("uv lock ... Distribution not found at
    # .../plugin-sources/startup-suite-platform-<id>"), with or without
    # `plugins remove` first. Run each @real test against a fresh root
    # (e.g. one container per test, `pytest -k <name>`); a second one fails
    # here, by name, instead of with that error.
    root = Path(_real_hermes("profile", "show", "default").stdout.split("Path:", 1)[-1].split()[0])
    marker = root / ".burrow-channel-real-test"
    if marker.exists():
        pytest.fail(f"a real-Hermes test already ran against {root}: {marker.read_text()}")
    marker.write_text(os.environ.get("PYTEST_CURRENT_TEST", "?") + "\n")
    name = "bct" + secrets.token_hex(4)
    made = _real_hermes("profile", "create", name, "--no-alias", "--no-skills")
    assert made.returncode == 0, made.stdout + made.stderr
    shown = _real_hermes("profile", "show", name).stdout
    home = Path(
        next(ln.split(":", 1)[1].strip() for ln in shown.splitlines() if ln.startswith("Path:"))
    )
    assert home.is_dir()
    (home / "config.yaml").write_text(PRE_CONFIG)
    (home / ".env").write_text(PRE_ENV)
    try:
        yield home
    finally:
        _real_hermes("plugins", "remove", "startup-suite-platform", home=home)
        _real_hermes("profile", "delete", "-y", name)


def _real_enabled(home: Path) -> list[str]:
    out = _real_hermes("config", "get", "plugins.enabled", "--json", home=home)
    return json.loads(out.stdout) if out.returncode == 0 else []


def _real_install(co: Path, home: Path, token: str):
    return install(co, home, _real_env(home), token)


def _real_update(co: Path, home: Path, *extra: str):
    return run(co / "update.sh", "--hermes-home", str(home), *extra, env=_real_env(home))


@real
@pytest.mark.xfail(
    strict=True,
    reason="pre-existing, measured at hermes-agent fdec926e: enabling the plugin moves Hermes PM "
    "to a new dependency environment, so the second install re-sets "
    "mcp_servers.startup-suite.command to the new environments/<id>/venv python",
)
def test_round_trip_with_the_real_hermes_cli(checkout, real_home, token):
    """Same round trip against hermes-agent itself: config.yaml returns to its
    original sha256 after install, reinstall and uninstall, and the reinstall
    sets nothing."""
    home = real_home
    pre_cfg, pre_env = sha(home / "config.yaml"), sha(home / ".env")
    first = _real_install(checkout, home, token)
    assert first.returncode == 0, first.stderr
    assert token not in (home / "config.yaml").read_text() + (home / ".env").read_text()
    second = _real_install(checkout, home, token)
    assert second.returncode == 0, second.stderr
    assert set_lines(second) == []
    un = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=_real_env(home))
    assert un.returncode == 0, un.stderr
    assert sha(home / "config.yaml") == pre_cfg, (home / "config.yaml").read_text()
    assert sha(home / ".env") == pre_env
    assert not token_path(home).exists() and not (home / ".suite-channel").exists()


def _pre_fix_install_sh() -> str | None:
    got = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{PRE_FIX}:install.sh"], capture_output=True, text=True
    )
    return got.stdout if got.returncode == 0 else None


@real
@pytest.mark.parametrize("which", ["dd199a01", "disable_cut"])
def test_real_hermes_declines_the_reinstall_without_the_disable_step(
    tmp_path, real_home, token, which
):
    """Positive control on the REAL CLI: an enabled plugin, upgraded by an
    installer that does not disable it first (dd199a01's own install.sh, and
    this one with the disable block cut out), is refused and stays on the old
    build."""
    if which == "dd199a01":
        text = _pre_fix_install_sh()
        if text is None:
            pytest.skip(f"{PRE_FIX} not in this clone")
    else:
        text = without_disable_step((ROOT / "install.sh").read_text())
    co = _make_checkout(tmp_path / "control", install_sh=text)
    assert _real_install(co, real_home, token).returncode == 0
    old_sha = _build(real_home)
    _commit_next(co)
    proc = _real_update(co, real_home)
    assert proc.returncode != 0
    assert "Reinstall declined: dependency install skipped (non-interactive)" in proc.stderr
    assert _build(real_home) == old_sha
    assert _real_enabled(real_home) == ["startup-suite-platform"]


@real
def test_real_hermes_update_over_an_enabled_plugin(checkout, real_home, token):
    assert _real_install(checkout, real_home, token).returncode == 0
    assert _real_enabled(real_home) == ["startup-suite-platform"]
    cfg_before = (real_home / "config.yaml").read_bytes()
    env_before = (real_home / ".env").read_bytes()
    new_sha = _commit_next(checkout)
    proc = _real_update(checkout, real_home)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == UPDATED
    assert _build(real_home) == new_sha
    assert (real_home / "plugins" / "startup-suite-platform" / "NEWFILE").is_file()
    assert _real_enabled(real_home) == ["startup-suite-platform"]
    assert (real_home / "config.yaml").read_bytes() == cfg_before
    assert (real_home / ".env").read_bytes() == env_before


@real
def test_real_hermes_refused_reinstall_re_enables_the_old_version(checkout, real_home, token):
    """Hermes refuses the new tree AFTER install.sh disabled the plugin (a
    plugin.yaml with a manifest_version this Hermes does not support): the
    run fails, and the plugin is enabled again at the build it had."""
    assert _real_install(checkout, real_home, token).returncode == 0
    old_sha = _build(real_home)
    cfg_before = (real_home / "config.yaml").read_bytes()
    manifest = checkout / "plugin.yaml"
    manifest.write_text("manifest_version: 999\n" + manifest.read_text())
    _git(checkout, "commit", "-q", "-am", "a manifest Hermes refuses")
    proc = _real_update(checkout, real_home)
    assert proc.returncode != 0
    assert "disabling plugin startup-suite-platform" in proc.stderr
    assert "manifest_version 999" in proc.stderr
    assert "re-enabled" in proc.stderr
    assert _build(real_home) == old_sha
    assert _real_enabled(real_home) == ["startup-suite-platform"]
    assert (real_home / "config.yaml").read_bytes() == cfg_before


def test_an_env_file_the_install_created_is_removed_when_empty(checkout, home, env, token):
    (home / ".env").unlink()
    assert install(checkout, home, env, token).returncode == 0
    assert (home / ".env").exists()
    _uninstall(checkout, home, env)
    assert not (home / ".env").exists()


# ------------------------------------------- BURROW_ names over a v0.2.0 host


# Exactly what v0.2.0's install.sh wrote after PRE_ENV (its own
# test_install_writes_env_plugin_and_mcp pinned these three lines).
def v020_env(home: Path, url: str = "wss://suite.example.test/runtime/ws") -> str:
    return (
        PRE_ENV + f"SUITE_URL={url}\n"
        "SUITE_RUNTIME_ID=rt-01a0d6c5\n"
        f"SUITE_RUNTIME_TOKEN_FILE={token_path(home)}\n"
    )


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def test_install_over_a_v020_env_rewrites_in_place_and_adds_burrow(checkout, home, env, token):
    old = v020_env(home)
    (home / ".env").write_text(old)
    proc = install(checkout, home, env, token)
    assert proc.returncode == 0, proc.stderr
    text = (home / ".env").read_text()
    # Every v0.2.0 line is still there, unchanged and in place; the BURROW_
    # twins are appended after them.
    assert text.startswith(old)
    assert text[len(old) :] == (
        "BURROW_URL=wss://suite.example.test/runtime/ws\n"
        "BURROW_RUNTIME_ID=rt-01a0d6c5\n"
        f"BURROW_RUNTIME_TOKEN_FILE={token_path(home)}\n"
    )
    from hermes_suite_channel.config import SuiteConfig

    cfg = SuiteConfig.from_extra({}, env=parse_env(text))
    assert (cfg.url, cfg.runtime_id, cfg.token) == (
        "wss://suite.example.test/runtime/ws",
        "rt-01a0d6c5",
        token,
    )
    again = install(checkout, home, env, token)
    assert again.returncode == 0 and (home / ".env").read_text() == text


def test_install_writes_both_names_for_the_user_flags(checkout, home, env, token):
    proc = install(checkout, home, env, token, "--allow-all-users", "--allowed-users", "Ryan,Jo")
    assert proc.returncode == 0, proc.stderr
    keys = parse_env((home / ".env").read_text())
    for suffix, value in (("ALLOW_ALL_USERS", "true"), ("ALLOWED_USERS", "Ryan,Jo")):
        assert keys[f"BURROW_{suffix}"] == keys[f"SUITE_{suffix}"] == value


def test_install_removes_a_legacy_token_under_either_name(checkout, home, env, token):
    (home / ".env").write_text(
        PRE_ENV + f"SUITE_RUNTIME_TOKEN={token}\nBURROW_RUNTIME_TOKEN={token}\n"
    )
    assert install(checkout, home, env, token).returncode == 0
    text = (home / ".env").read_text()
    assert token not in text
    assert "SUITE_RUNTIME_TOKEN=" not in text and "BURROW_RUNTIME_TOKEN=" not in text


def test_uninstall_removes_both_spellings_and_keeps_every_other_byte(checkout, home, env, token):
    unrelated = (
        PRE_ENV + "BURROW_UNRELATED=keep\nSUITE_URL_NOTES=keep\n"
        "  export   OTHER = spaced\nBURROW_URLX=keep\n"
    )
    (home / ".env").write_text(unrelated)
    assert install(checkout, home, env, token, "--allowed-users", "Ryan").returncode == 0
    with (home / ".env").open("a") as f:
        # Hand-added spellings of every key uninstall owns, both forms.
        f.write("BURROW_RUNTIME_TOKEN=x\nexport SUITE_ALLOW_ALL_USERS=true\n")
    _uninstall(checkout, home, env)
    assert (home / ".env").read_text() == unrelated


def test_uninstall_over_a_v020_env_gives_back_pre_env(checkout, home, env, token):
    assert install(checkout, home, env, token).returncode == 0
    (home / ".env").write_text(v020_env(home))  # as v0.2.0 left it
    _uninstall(checkout, home, env)
    assert (home / ".env").read_text() == PRE_ENV


async def test_v020_host_upgraded_by_update_sh_connects(checkout, home, env, token, monkeypatch):
    """THE upgrade contract: a host whose .env v0.2.0's install.sh wrote
    (SUITE_ names only) and whose config.yaml is unchanged, upgraded with this
    update.sh, keeps both files byte for byte and connects as the same
    runtime, through the real Hermes gateway adapter."""
    import sys as _sys

    from conftest import FAST
    from fake_phoenix import FakePhoenix
    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import parse_manifest_file

    async with FakePhoenix() as server:
        assert install(checkout, home, env, token).returncode == 0
        (home / ".env").write_text(v020_env(home, server.url))
        env_before = (home / ".env").read_bytes()
        cfg_before = (home / "config.yaml").read_bytes()

        (checkout / "NEWFILE").write_text("x\n")
        _git(checkout, "add", "NEWFILE")
        _git(checkout, "commit", "-q", "-m", "next")
        up = run(checkout / "update.sh", "--hermes-home", str(home), env=env)
        assert up.returncode == 0, up.stderr
        assert last_line(up) == UPDATED
        assert (home / ".env").read_bytes() == env_before
        assert (home / "config.yaml").read_bytes() == cfg_before

        for key in list(os.environ):
            if key.startswith(("SUITE_", "BURROW_")):
                monkeypatch.delenv(key)
        # What Hermes does at start: .env into os.environ (env_loader.py).
        for key, value in parse_env(env_before.decode()).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(home / "locks"))
        # Load the INSTALLED copy the way Hermes does, fresh module state.
        # Hermes namespaces a directory module by the first scope that loads
        # it (plugins_loader.py _BARE_MODULE_SCOPE); claim it in a throwaway map
        # so test_plugin_load still gets the bare name.
        from hermes_cli import plugins_loader

        monkeypatch.setattr(plugins_loader, "_BARE_MODULE_SCOPE", {})
        for name in [m for m in _sys.modules if m.startswith("hermes_plugins.startup_suite")]:
            monkeypatch.delitem(_sys.modules, name)
        plugin = home / "plugins" / "startup-suite-platform"
        manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
        manager = PluginManager()
        module = manager._load_directory_module(manifest)
        assert Path(module.__file__).resolve().parent == plugin.resolve()
        module.register(PluginContext(manifest, manager))
        entry = platform_registry.get("startup_suite")
        assert entry.check_fn() is True
        assert entry.allowed_users_env == "BURROW_ALLOWED_USERS"  # neither set: deny-all
        seed = entry.env_enablement_fn()
        adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={**seed, **FAST}))
        assert await adapter.connect() is True
        try:
            assert server.topic == "runtime:rt-01a0d6c5"
            assert any(f"token={token}" in p for p in server.request_paths)
        finally:
            await adapter.disconnect()
