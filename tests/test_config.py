import pytest

from hermes_suite_channel.config import ConfigError, SuiteConfig

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
