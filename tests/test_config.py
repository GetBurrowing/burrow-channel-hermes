import logging
import re
from pathlib import Path

import pytest

from hermes_suite_channel import config as config_mod
from hermes_suite_channel.config import (
    _TUNABLES,
    ENV_FALLBACKS,
    ConfigError,
    SuiteConfig,
    env_name_in_use,
    env_names_in_use,
    env_pair,
    resolve_env,
)

BASE = {"url": "wss://suite.example/runtime/ws", "runtime_id": "rt"}
ENV = {"SUITE_RUNTIME_TOKEN": "tok-cfg-01a0d6c5"}


def test_defaults_equal_the_claude_code_sibling():
    cfg = SuiteConfig.from_extra(BASE, env=ENV)
    # claude-code-suite-channel src/suite-client.ts
    assert cfg.reconnect_min_ms == 1_000  # DEFAULT_RECONNECT_MS
    assert cfg.reconnect_max_ms == 30_000  # DEFAULT_MAX_RECONNECT_MS
    assert cfg.stale_traffic_ms == 120_000  # STALE_TRAFFIC_MS
    assert cfg.watchdog_interval_ms == 30_000  # WATCHDOG_INTERVAL_MS
    assert cfg.push_timeout_ms == 10_000  # PUSH_TIMEOUT_MS
    assert cfg.heartbeat_ms == 30_000  # phoenix JS default heartbeatIntervalMs


def test_extra_keys_and_env_override():
    extra = {**BASE, "reconnect_min_ms": "500", "stale_traffic_ms": 9000, "reconnect_jitter": 0}
    env = {"SUITE_RECONNECT_MIN_MS": "250", "SUITE_RUNTIME_TOKEN": "tok-env-01a0d6c5"}
    cfg = SuiteConfig.from_extra(extra, env=env)
    assert cfg.reconnect_min_ms == 250
    assert cfg.stale_traffic_ms == 9000
    assert cfg.reconnect_jitter == 0.0
    assert cfg.token == "tok-env-01a0d6c5"


def test_repr_hides_token():
    cfg = SuiteConfig.from_extra(BASE, env=ENV)
    assert "tok-cfg-01a0d6c5" not in repr(cfg)


def test_missing_and_malformed_settings_name_the_key_not_the_value():
    with pytest.raises(ConfigError, match="token"):
        SuiteConfig.from_extra(BASE, env={})
    with pytest.raises(ConfigError, match="reconnect_max_ms") as info:
        SuiteConfig.from_extra({**BASE, "reconnect_max_ms": "tok-cfg-01a0d6c5"}, env=ENV)
    assert "tok-cfg-01a0d6c5" not in str(info.value)
    with pytest.raises(ConfigError, match="query"):
        SuiteConfig.from_extra({**BASE, "url": BASE["url"] + "?token=x"}, env=ENV)


def test_token_is_never_read_from_extra():
    # Hermes serialises `extra` in PlatformConfig.to_dict(); the token must not live there.
    with pytest.raises(ConfigError, match="token"):
        SuiteConfig.from_extra({**BASE, "token": "tok-extra-01a0d6c5"}, env={})


def test_token_file_wins_and_errors_name_the_path_not_the_content(tmp_path):
    tf = tmp_path / "tok"
    tf.write_text("tok-file-01a0d6c5\n")
    tf.chmod(0o600)
    env = {**ENV, "SUITE_RUNTIME_TOKEN_FILE": str(tf)}
    assert SuiteConfig.from_extra(BASE, env=env).token == "tok-file-01a0d6c5"
    tf.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600") as info:
        SuiteConfig.from_extra(BASE, env=env)
    assert "tok-file-01a0d6c5" not in str(info.value)
    with pytest.raises(ConfigError, match="cannot read token file"):
        SuiteConfig.from_extra(BASE, env={"SUITE_RUNTIME_TOKEN_FILE": str(tmp_path / "none")})


def test_default_token_file_then_env_then_fallback(tmp_path):
    default = tmp_path / "default"
    assert SuiteConfig.from_extra(BASE, env=ENV, default_token_file=default).token == (
        "tok-cfg-01a0d6c5"
    )
    default.write_text("tok-default-01a0d6c5")
    default.chmod(0o600)
    assert SuiteConfig.from_extra(BASE, env=ENV, default_token_file=default).token == (
        "tok-default-01a0d6c5"
    )
    cfg = SuiteConfig.from_extra(BASE, env={}, fallback_token="tok-fallback-01a0d6c5")
    assert cfg.token == "tok-fallback-01a0d6c5"


# ── BURROW_* first, SUITE_* fallback ─────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(config_mod, "_WARNED", set())


def test_env_fallbacks_is_exactly_this_raw_list():
    """GUARD. A raw literal, compared to a raw literal: never derived from the
    module (a derived list agrees with whatever the module says). Dropping or
    reordering a pair strands every host still on the old name. Mutation
    probe recorded in the PR: delete one pair from config.py, this fails."""
    assert ENV_FALLBACKS == (
        ("BURROW_URL", "SUITE_URL"),
        ("BURROW_RUNTIME_ID", "SUITE_RUNTIME_ID"),
        ("BURROW_RUNTIME_TOKEN", "SUITE_RUNTIME_TOKEN"),
        ("BURROW_RUNTIME_TOKEN_FILE", "SUITE_RUNTIME_TOKEN_FILE"),
        ("BURROW_RECONNECT_MIN_MS", "SUITE_RECONNECT_MIN_MS"),
        ("BURROW_RECONNECT_MAX_MS", "SUITE_RECONNECT_MAX_MS"),
        ("BURROW_RECONNECT_JITTER", "SUITE_RECONNECT_JITTER"),
        ("BURROW_STALE_TRAFFIC_MS", "SUITE_STALE_TRAFFIC_MS"),
        ("BURROW_WATCHDOG_INTERVAL_MS", "SUITE_WATCHDOG_INTERVAL_MS"),
        ("BURROW_HEARTBEAT_MS", "SUITE_HEARTBEAT_MS"),
        ("BURROW_PUSH_TIMEOUT_MS", "SUITE_PUSH_TIMEOUT_MS"),
        ("BURROW_JOIN_TIMEOUT_MS", "SUITE_JOIN_TIMEOUT_MS"),
        ("BURROW_DEDUPE_WINDOW_MS", "SUITE_DEDUPE_WINDOW_MS"),
        ("BURROW_TRUSTED_USER_IDS", "SUITE_TRUSTED_USER_IDS"),
        ("BURROW_ALLOWED_USERS", "SUITE_ALLOWED_USERS"),
        ("BURROW_ALLOW_ALL_USERS", "SUITE_ALLOW_ALL_USERS"),
        ("BURROW_HOME_CHANNEL", "SUITE_HOME_CHANNEL"),
        ("BURROW_AGENT_ID", "SUITE_AGENT_ID"),
    )


def test_no_reader_names_a_suite_variable_outside_env_fallbacks():
    """Every quoted SUITE_<X> name in the package source is the one inside
    ENV_FALLBACKS; a reader that spells it anywhere else bypasses the
    BURROW_-first rule. (Docstrings and messages carry the names unquoted.)"""
    pkg = Path(config_mod.__file__).parent
    hits = []
    for path in sorted(pkg.glob("*.py")):
        for m in re.finditer(r"[\"'](SUITE_[A-Z_]+)[\"']", path.read_text()):
            hits.append((path.name, m.group(1)))
    assert sorted(hits) == sorted(("config.py", old) for _new, old in ENV_FALLBACKS)


def test_every_new_name_is_the_old_name_with_the_prefix_swapped():
    for new, old in ENV_FALLBACKS:
        assert new.startswith("BURROW_") and old == "SUITE_" + new[len("BURROW_") :]


def test_env_pair_refuses_a_name_outside_the_list():
    with pytest.raises(KeyError):
        env_pair("BURROW_NOT_A_SETTING")


@pytest.mark.parametrize("pair", ENV_FALLBACKS, ids=[new for new, _ in ENV_FALLBACKS])
def test_resolve_every_pair_old_new_both_neither(pair, caplog):
    caplog.set_level(logging.WARNING)
    new, old = pair
    assert resolve_env({old: "o"}, pair) == ("o", old)  # old only
    assert resolve_env({new: "n"}, pair) == ("n", new)  # new only
    assert resolve_env({new: "n", old: "o"}, pair) == ("n", new)  # both differ: new wins
    assert resolve_env({}, pair) == (None, None)  # neither
    assert resolve_env({new: "  ", old: "o"}, pair) == ("o", old)  # blank new is unset
    assert env_name_in_use({old: "o"}, pair) == old
    assert env_name_in_use({}, pair) == new
    assert env_names_in_use({new: "n", old: "o"}, pair) == [new, old]
    assert env_names_in_use({}, pair) == [new]
    warned = [r.getMessage() for r in caplog.records]
    assert warned == [f"Burrow: {old} is deprecated; set {new} (the old name still works)"]


def _cfg(env):
    return SuiteConfig.from_extra({}, env=env)


REQ_OLD = {"SUITE_URL": "wss://old/runtime/ws", "SUITE_RUNTIME_ID": "rt-old"}
REQ_OLD["SUITE_RUNTIME_TOKEN"] = "tok-old-01a14b9f"
REQ_NEW = {k.replace("SUITE_", "BURROW_"): v.replace("old", "new") for k, v in REQ_OLD.items()}


def test_required_settings_old_new_both_neither():
    cfg = _cfg(REQ_OLD)
    assert (cfg.url, cfg.runtime_id, cfg.token) == (
        "wss://old/runtime/ws",
        "rt-old",
        "tok-old-01a14b9f",
    )
    cfg = _cfg(REQ_NEW)
    assert (cfg.url, cfg.runtime_id, cfg.token) == (
        "wss://new/runtime/ws",
        "rt-new",
        "tok-new-01a14b9f",
    )
    cfg = _cfg({**REQ_OLD, **REQ_NEW})
    assert (cfg.url, cfg.runtime_id, cfg.token) == (
        "wss://new/runtime/ws",
        "rt-new",
        "tok-new-01a14b9f",
    )
    for key in ("url", "runtime_id", "token"):
        with pytest.raises(ConfigError, match=key):
            _cfg({k: v for k, v in REQ_NEW.items() if not k.endswith(key.upper())})


def test_token_file_old_new_both_neither(tmp_path):
    files = {}
    for tag in ("old", "new"):
        files[tag] = tmp_path / tag
        files[tag].write_text(f"tok-{tag}file-01a14b9f\n")
        files[tag].chmod(0o600)
    base = {"BURROW_URL": "wss://h/runtime/ws", "BURROW_RUNTIME_ID": "rt"}
    old = {"SUITE_RUNTIME_TOKEN_FILE": str(files["old"])}
    new = {"BURROW_RUNTIME_TOKEN_FILE": str(files["new"])}
    assert _cfg({**base, **old}).token == "tok-oldfile-01a14b9f"
    assert _cfg({**base, **new}).token == "tok-newfile-01a14b9f"
    assert _cfg({**base, **old, **new}).token == "tok-newfile-01a14b9f"
    with pytest.raises(ConfigError, match="token"):
        _cfg(base)


@pytest.mark.parametrize("key", list(_TUNABLES))
def test_every_tunable_old_new_both_neither(key):
    (new, old), typ, default = _TUNABLES[key]
    a, b = ("0.5", "0.25") if typ is float else ("40000", "50000")
    if key == "reconnect_min_ms":
        a, b = "400", "500"  # must stay <= the default reconnect_max_ms
    assert getattr(_cfg({**REQ_NEW, old: a}), key) == typ(a)
    assert getattr(_cfg({**REQ_NEW, new: b}), key) == typ(b)
    assert getattr(_cfg({**REQ_NEW, old: a, new: b}), key) == typ(b)
    assert getattr(_cfg(REQ_NEW), key) == default


def test_trusted_user_ids_old_new_both_neither():
    u1, u2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    assert _cfg({**REQ_NEW, "SUITE_TRUSTED_USER_IDS": u1}).trusted_user_ids == {u1}
    assert _cfg({**REQ_NEW, "BURROW_TRUSTED_USER_IDS": u2}).trusted_user_ids == {u2}
    both = {**REQ_NEW, "SUITE_TRUSTED_USER_IDS": u1, "BURROW_TRUSTED_USER_IDS": u2}
    assert _cfg(both).trusted_user_ids == {u2}
    assert _cfg(REQ_NEW).trusted_user_ids == frozenset()


def test_deprecation_warning_is_once_per_name_and_never_a_value(caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        _cfg(REQ_OLD)
    lines = [r.getMessage() for r in caplog.records]
    assert sorted(lines) == sorted(
        f"Burrow: SUITE_{n} is deprecated; set BURROW_{n} (the old name still works)"
        for n in ("URL", "RUNTIME_ID", "RUNTIME_TOKEN")
    )
    assert not any("tok-old-01a14b9f" in line or "wss://old" in line for line in lines)


def test_no_warning_when_only_burrow_names_are_set(caplog):
    caplog.set_level(logging.WARNING)
    _cfg(REQ_NEW)
    assert caplog.records == []
