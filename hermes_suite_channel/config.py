"""Connection settings, read from a Hermes ``PlatformConfig.extra`` dict and env.

Environment variables override ``extra`` (the convention Hermes's own IRC
adapter uses). The tunables are an experiment surface: the reconnect curve
and the stale-traffic watchdog can be changed per install without a code
change. Defaults equal burrow-channel-claude-code src/suite-client.ts
(``DEFAULT_RECONNECT_MS``, ``DEFAULT_MAX_RECONNECT_MS``, ``STALE_TRAFFIC_MS``,
``WATCHDOG_INTERVAL_MS``, ``PUSH_TIMEOUT_MS``). Jitter is new here; the
sibling has none.

Every env name has two spellings: ``BURROW_<X>``, read first, and the
deprecated ``SUITE_<X>``, read when the new one is unset or blank. Using the
old one logs one deprecation warning per name per process. ``ENV_FALLBACKS``
is the whole list; every reader goes through ``resolve_env``.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import RedactingLogger

_log = RedactingLogger(logging.getLogger(__name__))

# (new name, deprecated name). A RAW literal on purpose: tests/test_config.py
# pins it against a second literal, so dropping a pair fails a test instead of
# silently stranding the hosts that still set the old name.
ENV_FALLBACKS: tuple[tuple[str, str], ...] = (
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

EnvPair = tuple[str, str]
_PAIRS: dict[str, EnvPair] = {new: (new, old) for new, old in ENV_FALLBACKS}
_WARNED: set[str] = set()


def env_pair(new_name: str) -> EnvPair:
    """The ``(new, old)`` pair for ``new_name``; KeyError for a name not in
    ``ENV_FALLBACKS``, so a reader cannot bypass the list."""
    return _PAIRS[new_name]


def _set(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def warn_deprecated(old: str, new: str) -> None:
    """One warning per deprecated name per process. Names only, never values."""
    if old in _WARNED:
        return
    _WARNED.add(old)
    _log.warning("Burrow: %s is deprecated; set %s (the old name still works)", old, new)


def resolve_env(env: Mapping[str, Any], pair: EnvPair) -> tuple[Any, str | None]:
    """``(value, name it came from)``: the new name when set and non-blank,
    else the deprecated one (with a one-time warning), else ``(None, None)``."""
    new, old = pair
    value = env.get(new)
    if _set(value):
        return value, new
    value = env.get(old)
    if _set(value):
        warn_deprecated(old, new)
        return value, old
    return None, None


def env_value(env: Mapping[str, Any], pair: EnvPair) -> Any:
    return resolve_env(env, pair)[0]


def env_name_in_use(env: Mapping[str, Any], pair: EnvPair) -> str:
    """The spelling a host actually sets, for APIs that take one env NAME and
    read it themselves (Hermes's allowlist, allow-all and cron home-channel
    gates). The new name when neither is set."""
    return resolve_env(env, pair)[1] or pair[0]


def env_names_in_use(env: Mapping[str, Any], pair: EnvPair) -> list[str]:
    """Every spelling a host sets (new first), or ``[new]`` when neither is.
    For Hermes's ``required_env``, which a profile clone strips name by name:
    a host written by install.sh sets both, and both must go."""
    names = [n for n in pair if _set(env.get(n))]
    if names and names[0] != pair[0]:
        warn_deprecated(pair[1], pair[0])
    return names or [pair[0]]


# key in `extra` -> (env var, type, default)
_TUNABLES: dict[str, tuple[EnvPair, type, Any]] = {
    "reconnect_min_ms": (env_pair("BURROW_RECONNECT_MIN_MS"), int, 1_000),
    "reconnect_max_ms": (env_pair("BURROW_RECONNECT_MAX_MS"), int, 30_000),
    "reconnect_jitter": (env_pair("BURROW_RECONNECT_JITTER"), float, 0.2),
    "stale_traffic_ms": (env_pair("BURROW_STALE_TRAFFIC_MS"), int, 120_000),
    "watchdog_interval_ms": (env_pair("BURROW_WATCHDOG_INTERVAL_MS"), int, 30_000),
    "heartbeat_ms": (env_pair("BURROW_HEARTBEAT_MS"), int, 30_000),
    "push_timeout_ms": (env_pair("BURROW_PUSH_TIMEOUT_MS"), int, 10_000),
    "join_timeout_ms": (env_pair("BURROW_JOIN_TIMEOUT_MS"), int, 10_000),
    "dedupe_window_ms": (env_pair("BURROW_DEDUPE_WINDOW_MS"), int, 1_000),
}

URL_ENV = env_pair("BURROW_URL")
RUNTIME_ID_ENV = env_pair("BURROW_RUNTIME_ID")
TOKEN_ENV = env_pair("BURROW_RUNTIME_TOKEN")
TOKEN_FILE_ENV = env_pair("BURROW_RUNTIME_TOKEN_FILE")

_REQUIRED: dict[str, EnvPair] = {
    "url": URL_ENV,
    "runtime_id": RUNTIME_ID_ENV,
    "token": TOKEN_ENV,
}
# Where install.sh puts the token, relative to the (profile's) Hermes home.
# mcp-tokens/ is Hermes's own credential directory: its file tools, media
# delivery, context references and dashboard file router all refuse to read
# it (agent/file_safety.py _READ_DENIED_DIRS, gateway/platforms/base.py), the
# same protection $HERMES_HOME/.env gets. Hermes names its own files there
# `<server>.json` / `.client.json` / `.meta.json`, so this name cannot collide.
TOKEN_FILE_RELPATH = Path("mcp-tokens") / "burrow-platform.runtime-token"

# Read from env ONLY, never from ``extra``. Hermes serialises a platform's
# ``extra`` dict in ``PlatformConfig.to_dict()`` (gateway/config.py), which
# ``GatewayConfig.to_dict()`` nests; a token in ``extra`` (or in the typed
# ``PlatformConfig.token`` field) would ride along wherever that dict goes.
_ENV_ONLY = frozenset({"token"})


# Burrow user ids whose messages are delivered as owner instructions rather than
# inside the untrusted envelope. Env first, then ``extra["trusted_user_ids"]``
# (a comma string or a list). Empty, the default, trusts nobody. Only canonical
# UUIDs are kept: an entry of any other shape (a display name, a typo) is
# dropped, so a misconfiguration trusts LESS, never more.
TRUSTED_USER_IDS_ENV = env_pair("BURROW_TRUSTED_USER_IDS")
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
    BURROW_RUNTIME_TOKEN_FILE (or SUITE_RUNTIME_TOKEN_FILE), ``default_token_file``
    when it exists, or the legacy BURROW_RUNTIME_TOKEN (or SUITE_RUNTIME_TOKEN)
    variable. "" when none is set.

    The file forms keep the token out of every process environment. The
    variable form cannot: Hermes copies $HERMES_HOME/.env into os.environ at
    start, on every gateway turn and before every MCP config load
    (hermes_cli/env_loader.py load_hermes_dotenv), and children spawned
    without a scrub inherit it. See README "Security notes".
    """
    named = env_value(env, TOKEN_FILE_ENV)
    if named:
        return read_token_file(str(named).strip())
    if default_token_file is not None and os.path.lexists(default_token_file):
        return read_token_file(default_token_file)
    return env_value(env, TOKEN_ENV) or ""


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
        for key, pair in _REQUIRED.items():
            if key == "token":
                values[key] = resolve_token(env, default_token_file) or fallback_token or ""
                continue
            from_extra = "" if key in _ENV_ONLY else extra.get(key)
            values[key] = env_value(env, pair) or from_extra or ""
        for key, (pair, typ, default) in _TUNABLES.items():
            raw = env_value(env, pair)
            if raw is None:
                raw = extra.get(key, default)
            try:
                values[key] = typ(raw)
            except (TypeError, ValueError):
                raise ConfigError(f"setting {key} must be {typ.__name__}") from None
        raw_trusted = env_value(env, TRUSTED_USER_IDS_ENV)
        if raw_trusted is None:
            raw_trusted = extra.get("trusted_user_ids")
        trusted, dropped = parse_trusted_user_ids(raw_trusted)
        values["trusted_user_ids"] = trusted
        values["trusted_user_ids_dropped"] = dropped
        return cls(**values)
