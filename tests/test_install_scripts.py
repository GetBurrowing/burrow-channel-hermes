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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUB_DIR = Path(__file__).resolve().parent / "stub_hermes"
SCRIPTS = ("install.sh", "uninstall.sh", "update.sh")

INSTALLED = "hermes-suite-channel: installed"
UNINSTALLED = "hermes-suite-channel: uninstalled"
UPDATED = "hermes-suite-channel: updated"

PRE_CONFIG = "# user config, hand written\nmodel:\n  default: qwen3.5-9b  # trailing comment\n"
PRE_ENV = 'OPENAI_API_KEY=dummy-not-a-key\nexport FOO="bar baz"\n# a comment line\n'


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
        **os.environ,
        "PATH": f"{STUB_DIR}{os.pathsep}{os.environ['PATH']}",
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


# ------------------------------------------------------------------ install


def test_install_writes_env_plugin_and_mcp(checkout, home, env, token):
    proc = install(checkout, home, env, token)
    assert proc.returncode == 0, proc.stderr
    assert last_line(proc) == INSTALLED

    env_text = (home / ".env").read_text()
    assert env_text.startswith(PRE_ENV)
    assert env_text[len(PRE_ENV) :] == (
        "SUITE_URL=wss://suite.example.test/runtime/ws\n"
        "SUITE_RUNTIME_ID=rt-01a0d6c5\n"
        f"SUITE_RUNTIME_TOKEN={token}\n"
    )
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600

    plugin = home / "plugins" / "startup-suite-platform"
    assert (plugin / "plugin.yaml").is_file()
    sha = _git(checkout, "rev-parse", "HEAD")
    assert (plugin / "hermes_suite_channel" / "_build.py").read_text().count(
        f'BUILD = "{sha}"'
    ) == 1

    cfg = (home / "config.yaml").read_text()
    assert cfg.startswith(PRE_CONFIG)
    assert '# hermes-stub: mcp_servers.startup-suite.url = "https://suite.example.test/mcp"' in cfg
    assert (
        "# hermes-stub: mcp_servers.startup-suite.headers.Authorization = "
        '"Bearer ${SUITE_RUNTIME_TOKEN}"' in cfg
    )
    assert '# hermes-stub: plugins.enabled = ["startup-suite-platform"]' in cfg
    assert token not in cfg

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
    mutating = [
        c
        for c in stub_calls(env)[n_calls:]
        if c[:2] in (["plugins", "install"], ["plugins", "enable"], ["config", "set"])
    ]
    assert mutating == []


def test_round_trip_restores_every_preexisting_byte(checkout, home, env, token):
    before = snapshot(home)
    assert install(checkout, home, env, token).returncode == 0
    assert install(checkout, home, env, token).returncode == 0

    un = run(checkout / "uninstall.sh", "--hermes-home", str(home), env=env)
    assert un.returncode == 0, un.stderr
    assert last_line(un) == UNINSTALLED

    after = snapshot(home)
    assert {k: after.get(k) for k in before} == before
    assert not (home / "plugins" / "startup-suite-platform").exists()
    assert set(after) - set(before) == set()

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
    assert f"SUITE_RUNTIME_TOKEN={token}\n" in (home / ".env").read_text()


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

    tf = tmp_path / "tok"
    tf.write_text(token)
    ok = subprocess.run(
        [*base, "--hermes-home", str(home), "--token-file", str(tf), "--repo", str(checkout)],
        input=script,
        **kw,
    )
    assert ok.returncode == 0, ok.stderr
    assert f"SUITE_RUNTIME_TOKEN={token}\n" in (home / ".env").read_text()


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
    anchor = '  env_set SUITE_RUNTIME_TOKEN "$TOKEN"\n'
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
