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
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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

TOKEN_ENV = "SUITE_RUNTIME_TOKEN"
TOKEN_FILE_ENV = "SUITE_RUNTIME_TOKEN_FILE"
# Where install.sh puts the token, relative to the (profile's) Hermes home.
# mcp-tokens/ is Hermes's own credential directory: its file tools, media
# delivery, context references and dashboard file router all refuse to read
# it (agent/file_safety.py _READ_DENIED_DIRS, gateway/platforms/base.py), the
# same protection $HERMES_HOME/.env gets. Hermes names its own files there
# `<server>.json` / `.client.json` / `.meta.json`, so this name cannot collide.
TOKEN_FILE_RELPATH = Path("mcp-tokens") / "startup-suite-platform.runtime-token"

# Read from env ONLY, never from ``extra``. Hermes serialises a platform's
# ``extra`` dict in ``PlatformConfig.to_dict()`` (gateway/config.py), which
# ``GatewayConfig.to_dict()`` nests; a token in ``extra`` (or in the typed
# ``PlatformConfig.token`` field) would ride along wherever that dict goes.
_ENV_ONLY = frozenset({"token"})


# Suite user ids whose messages are delivered as owner instructions rather than
# inside the untrusted envelope. Env first, then ``extra["trusted_user_ids"]``
# (a comma string or a list). Empty, the default, trusts nobody. Only canonical
# UUIDs are kept: an entry of any other shape (a display name, a typo) is
# dropped, so a misconfiguration trusts LESS, never more.
TRUSTED_USER_IDS_ENV = "SUITE_TRUSTED_USER_IDS"
_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def parse_trusted_user_ids(raw: Any) -> tuple[frozenset[str], int]:
    """``(kept ids, number of entries dropped as not-a-UUID)``. Blank entries
    are skipped silently; ids are lower-cased (core emits lower case)."""
    if raw is None:
        return frozenset(), 0
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        items = [raw]
    kept: set[str] = set()
    dropped = 0
    for item in items:
        text = item.strip().lower() if isinstance(item, str) else None
        if text == "":
            continue
        if text is not None and _UUID.match(text):
            kept.add(text)
        else:
            dropped += 1
    return frozenset(kept), dropped


class ConfigError(ValueError):
    """A missing or malformed setting. Messages name keys, never values."""


def read_token_file(path: str | os.PathLike) -> str:
    """The token held in ``path``: first line, surrounding whitespace removed.

    Refused when group or other can read the file (the ssh rule): a token file
    another local user can read is a leaked token. Errors name the path, never
    the content.
    """
    try:
        with open(path, encoding="utf-8") as f:
            mode = os.fstat(f.fileno()).st_mode
            first = f.readline()
    except OSError as exc:
        raise ConfigError(f"cannot read token file {path}: {type(exc).__name__}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"token file {path} is readable by group or other; chmod 600 it")
    return first.strip()


def resolve_token(
    env: Mapping[str, str], default_token_file: str | os.PathLike | None = None
) -> str:
    """The runtime token, from (in order) the file named by
    SUITE_RUNTIME_TOKEN_FILE, ``default_token_file`` when it exists, or the
    legacy SUITE_RUNTIME_TOKEN variable. "" when none is set.

    The file forms keep the token out of every process environment. The
    variable form cannot: Hermes copies $HERMES_HOME/.env into os.environ at
    start, on every gateway turn and before every MCP config load
    (hermes_cli/env_loader.py load_hermes_dotenv), and children spawned
    without a scrub inherit it. See README "Security notes".
    """
    named = env.get(TOKEN_FILE_ENV)
    if named:
        return read_token_file(named)
    if default_token_file is not None and os.path.lexists(default_token_file):
        return read_token_file(default_token_file)
    return env.get(TOKEN_ENV) or ""


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
    trusted_user_ids: frozenset[str] = frozenset()
    trusted_user_ids_dropped: int = 0

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
        cls,
        extra: Mapping[str, Any] | None,
        env: Mapping[str, str] | None = None,
        *,
        default_token_file: str | os.PathLike | None = None,
        fallback_token: str | None = None,
    ) -> SuiteConfig:
        """``fallback_token`` is used only when no file or variable names one
        (the adapter passes the value it took out of os.environ at load)."""
        extra = extra or {}
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        for key, env_name in _REQUIRED.items():
            if key == "token":
                values[key] = resolve_token(env, default_token_file) or fallback_token or ""
                continue
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
        raw_trusted = env.get(TRUSTED_USER_IDS_ENV)
        if raw_trusted in (None, ""):
            raw_trusted = extra.get("trusted_user_ids")
        trusted, dropped = parse_trusted_user_ids(raw_trusted)
        values["trusted_user_ids"] = trusted
        values["trusted_user_ids_dropped"] = dropped
        return cls(**values)
