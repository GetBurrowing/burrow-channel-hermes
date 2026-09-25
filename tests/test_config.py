import pytest

from hermes_suite_channel.config import ConfigError, SuiteConfig

BASE = {"url": "wss://suite.example/runtime/ws", "runtime_id": "rt", "token": "tok-cfg-01a0d6c5"}


def test_defaults_equal_the_claude_code_sibling():
    cfg = SuiteConfig.from_extra(BASE, env={})
    # claude-code-suite-channel src/suite-client.ts
    assert cfg.reconnect_min_ms == 1_000  # DEFAULT_RECONNECT_MS
    assert cfg.reconnect_max_ms == 30_000  # DEFAULT_MAX_RECONNECT_MS
    assert cfg.stale_traffic_ms == 120_000  # STALE_TRAFFIC_MS
    assert cfg.watchdog_interval_ms == 30_000  # WATCHDOG_INTERVAL_MS
    assert cfg.push_timeout_ms == 10_000  # PUSH_TIMEOUT_MS
    assert cfg.heartbeat_ms == 30_000  # phoenix JS default heartbeatIntervalMs


def test_extra_keys_and_env_override():
    extra = {**BASE, "reconnect_min_ms": "500", "stale_traffic_ms": 9000, "reconnect_jitter": 0}
    env = {"SUITE_RECONNECT_MIN_MS": "250", "SUITE_TOKEN": "tok-env-01a0d6c5"}
    cfg = SuiteConfig.from_extra(extra, env=env)
    assert cfg.reconnect_min_ms == 250
    assert cfg.stale_traffic_ms == 9000
    assert cfg.reconnect_jitter == 0.0
    assert cfg.token == "tok-env-01a0d6c5"


def test_repr_hides_token():
    cfg = SuiteConfig.from_extra(BASE, env={})
    assert "tok-cfg-01a0d6c5" not in repr(cfg)


def test_missing_and_malformed_settings_name_the_key_not_the_value():
    with pytest.raises(ConfigError, match="token"):
        SuiteConfig.from_extra({**BASE, "token": ""}, env={})
    with pytest.raises(ConfigError, match="reconnect_max_ms") as info:
        SuiteConfig.from_extra({**BASE, "reconnect_max_ms": "tok-cfg-01a0d6c5"}, env={})
    assert "tok-cfg-01a0d6c5" not in str(info.value)
    with pytest.raises(ConfigError, match="query"):
        SuiteConfig.from_extra({**BASE, "url": BASE["url"] + "?token=x"}, env={})
