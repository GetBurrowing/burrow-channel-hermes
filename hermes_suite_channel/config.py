"""Connection settings, read from a Hermes ``PlatformConfig.extra`` dict and env.

Environment variables override ``extra`` (the convention Hermes's own IRC
adapter uses). The tunables are an experiment surface: the reconnect curve
and the stale-traffic watchdog can be changed per install without a code
change. Defaults equal claude-code-suite-channel src/suite-client.ts
(``DEFAULT_RECONNECT_MS``, ``DEFAULT_MAX_RECONNECT_MS``, ``STALE_TRAFFIC_MS``,
``WATCHDOG_INTERVAL_MS``, ``PUSH_TIMEOUT_MS``). Jitter is new here; the
sibling has none.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# key in `extra` -> (env var, type, default)
_TUNABLES: dict[str, tuple[str, type, Any]] = {
    "reconnect_min_ms": ("SUITE_RECONNECT_MIN_MS", int, 1_000),
    "reconnect_max_ms": ("SUITE_RECONNECT_MAX_MS", int, 30_000),
    "reconnect_jitter": ("SUITE_RECONNECT_JITTER", float, 0.2),
    "stale_traffic_ms": ("SUITE_STALE_TRAFFIC_MS", int, 120_000),
    "watchdog_interval_ms": ("SUITE_WATCHDOG_INTERVAL_MS", int, 30_000),
    "heartbeat_ms": ("SUITE_HEARTBEAT_MS", int, 30_000),
    "push_timeout_ms": ("SUITE_PUSH_TIMEOUT_MS", int, 10_000),
    "join_timeout_ms": ("SUITE_JOIN_TIMEOUT_MS", int, 10_000),
    "dedupe_window_ms": ("SUITE_DEDUPE_WINDOW_MS", int, 1_000),
}

_REQUIRED: dict[str, str] = {
    "url": "SUITE_URL",
    "runtime_id": "SUITE_RUNTIME_ID",
    "token": "SUITE_RUNTIME_TOKEN",
}

# Read from env ONLY, never from ``extra``. Hermes serialises a platform's
# ``extra`` dict in ``PlatformConfig.to_dict()`` (gateway/config.py), which
# ``GatewayConfig.to_dict()`` nests; a token in ``extra`` (or in the typed
# ``PlatformConfig.token`` field) would ride along wherever that dict goes.
_ENV_ONLY = frozenset({"token"})


class ConfigError(ValueError):
    """A missing or malformed setting. Messages name keys, never values."""


@dataclass(frozen=True)
class SuiteConfig:
    url: str
    runtime_id: str
    # repr=False: a config object printed in a log line or a traceback must
    # not carry the token.
    token: str = field(repr=False)
    reconnect_min_ms: int = 1_000
    reconnect_max_ms: int = 30_000
    # Fraction of each backoff delay removed at random: delay * (1 - j*U[0,1)).
    # Subtractive, so a jittered delay never exceeds reconnect_max_ms.
    reconnect_jitter: float = 0.2
    stale_traffic_ms: int = 120_000
    watchdog_interval_ms: int = 30_000
    heartbeat_ms: int = 30_000
    push_timeout_ms: int = 10_000
    join_timeout_ms: int = 10_000
    dedupe_window_ms: int = 1_000

    def __post_init__(self) -> None:
        for key in _REQUIRED:
            if not getattr(self, key):
                raise ConfigError(f"missing required setting: {key}")
        if "?" in self.url or "#" in self.url:
            # socket_url() owns the query string; a second one would split the
            # token across two places.
            raise ConfigError("url must not carry a query string or fragment")
        if self.reconnect_min_ms <= 0 or self.reconnect_max_ms < self.reconnect_min_ms:
            raise ConfigError("need 0 < reconnect_min_ms <= reconnect_max_ms")
        if not 0.0 <= self.reconnect_jitter <= 1.0:
            raise ConfigError("reconnect_jitter must be within [0, 1]")

    @classmethod
    def from_extra(
        cls, extra: Mapping[str, Any] | None, env: Mapping[str, str] | None = None
    ) -> SuiteConfig:
        extra = extra or {}
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        for key, env_name in _REQUIRED.items():
            from_extra = "" if key in _ENV_ONLY else extra.get(key)
            values[key] = env.get(env_name) or from_extra or ""
        for key, (env_name, typ, default) in _TUNABLES.items():
            raw = env.get(env_name)
            if raw in (None, ""):
                raw = extra.get(key, default)
            try:
                values[key] = typ(raw)
            except (TypeError, ValueError):
                raise ConfigError(f"setting {key} must be {typ.__name__}") from None
        return cls(**values)
