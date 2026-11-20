"""Move a Hermes home's session state from the pre-v0.5.0 platform key to ``burrow``.

Until v0.5.0 the plugin registered the gateway platform ``startup_suite``.
Hermes bakes that key into durable state, so a plain rename of
``adapter.PLATFORM_NAME`` would orphan every existing chat: the routing index
row for ``agent:<ns>:startup_suite:<chat_type>:<chat_id>[...]`` is never looked
up again (the new gateway builds ``agent:<ns>:burrow:...`` keys), and a gateway
that loads a row whose ``platform`` it does not know skips it with a warning
and then DELETES it on its next whole-index save (``replace_gateway_routing_
entries``). The chat's next message then starts a fresh session.

What this module rewrites, in ONE ``SessionDB._execute_write`` transaction
(all or nothing), with the same shape-level SQL Hermes's own
``rekey_profile_state`` uses for a profile rename (hermes_state_gateway.py at
fdec926e):

* ``gateway_routing``: ``session_key`` and, inside ``entry_json``,
  ``session_key``, ``platform``, ``origin.platform``, and a renamed chat id;
* ``sessions``: ``source``, ``session_key``, ``origin_json`` (as above),
  ``handoff_platform``, and a renamed ``chat_id`` / ``display_name``;
* ``delivery_obligations``: ``platform``, ``session_key``, ``chat_id``;
* ``gateway_hygiene_state``, ``conversation_generations`` (``source`` too)
  and ``async_delegations.origin_session``: the key.

And two files Hermes keeps beside it, each staged in full before state.db
commits and renamed into place after:

* ``sessions/sessions.json``, the legacy routing mirror. The gateway imports
  every key it holds that state.db lacks, so an unmigrated mirror would put
  the OLD keys back into the index at the next start;
* ``cron/jobs.json``: a job's ``deliver`` / ``failure_deliver`` targets
  (``startup_suite`` or ``startup_suite:<chat>``) and ``origin.platform``.

Every rewrite is keyed on exact segments, never on a substring: a session key
is ``agent:<ns>:<platform>:<rest>`` (gateway/session.py ``build_session_key``;
a profile id never holds ``:``), so only a key whose third segment is
``startup_suite`` moves. A target key that already exists refuses the whole
migration (``RouteCollision``) having changed nothing, as
``rekey_profile_state`` does.

ONLY WITH THE GATEWAY STOPPED. A running gateway holds the routing index in
memory and writes it back over any change made here (the reason Hermes's own
``rekey_profile_routing`` rekeys the live store instead). ``migrate_home``
asks Hermes (``gateway.status.is_gateway_running``, scoped to this home's
``gateway.pid``) and refuses with ``GatewayRunning`` when one is up and there
is anything to move. burrow-cli runs this on ``burrow hermes``'s start path,
which already refuses to rename under a live session; see README "Platform
rename" for running it by hand.

Idempotent: a second run finds nothing to move and reports zeros.

Run as ``python -m hermes_burrow_channel.platform_rename --hermes-home DIR``
with Hermes's interpreter and its modules importable. Prints ONE JSON object
on stdout; exit 0 moved (or nothing to move), 2 refused (gateway running, or
a collision) having changed nothing, 1 any other failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

OLD_PLATFORM = "startup_suite"
# Equal to adapter.PLATFORM_NAME (pinned by tests/test_platform_rename.py; not
# imported, so this module runs without loading the adapter).
NEW_PLATFORM = "burrow"
# Chat ids the adapter itself minted under the old names. Only the adapter's
# own sentinel; a Burrow space id is a UUID and never changes.
CHAT_ID_RENAMES: dict[str, str] = {"suite:no-space": "burrow:no-space"}

RESULT_TAG = "burrow-channel-hermes platform_rename"


class GatewayRunning(RuntimeError):
    """A gateway is running for this home, and there is state to move."""


class RouteCollision(ValueError):
    """A renamed key already exists; nothing was changed."""


# ── pure rewrites ─────────────────────────────────────────────────────────


def rekey(session_key: Any) -> str | None:
    """The new key for an old-platform session key, else None (not ours, or
    already moved). ``agent:<ns>:startup_suite:<chat_type>:<chat_id>...`` ->
    ``agent:<ns>:burrow:<chat_type>:<renamed chat_id>...``."""
    if not isinstance(session_key, str):
        return None
    parts = session_key.split(":", 3)
    if len(parts) < 3 or parts[0] != "agent" or parts[2] != OLD_PLATFORM:
        return None
    rest = parts[3] if len(parts) == 4 else ""
    if rest:
        chat_type, sep, tail = rest.partition(":")
        for old_id, new_id in CHAT_ID_RENAMES.items():
            if tail == old_id or tail.startswith(old_id + ":"):
                tail = new_id + tail[len(old_id) :]
                break
        rest = chat_type + sep + tail
    return ":".join([parts[0], parts[1], NEW_PLATFORM] + ([rest] if rest else []))


def _chat(value: Any) -> Any:
    return CHAT_ID_RENAMES.get(value, value) if isinstance(value, str) else value


def rewrite_origin(origin: Any) -> bool:
    """Rewrite a SessionSource dict in place. True when it changed."""
    if not isinstance(origin, dict) or origin.get("platform") != OLD_PLATFORM:
        return False
    origin["platform"] = NEW_PLATFORM
    for field in ("chat_id", "chat_name"):
        if field in origin:
            origin[field] = _chat(origin[field])
    return True


def rewrite_entry(entry: Any) -> bool:
    """Rewrite a SessionEntry dict (``gateway_routing.entry_json``, a
    sessions.json value) in place. True when it changed."""
    if not isinstance(entry, dict):
        return False
    changed = False
    new_key = rekey(entry.get("session_key"))
    if new_key is not None:
        entry["session_key"] = new_key
        changed = True
    if entry.get("platform") == OLD_PLATFORM:
        entry["platform"] = NEW_PLATFORM
        changed = True
    if "display_name" in entry and _chat(entry["display_name"]) != entry["display_name"]:
        entry["display_name"] = _chat(entry["display_name"])
        changed = True
    return rewrite_origin(entry.get("origin")) or changed


def rewrite_target(target: Any) -> Any:
    """A cron ``deliver`` value: ``startup_suite``, ``startup_suite:<chat>``,
    a comma-separated list of those, or a list. Other targets are kept."""
    if isinstance(target, list):
        return [rewrite_target(t) for t in target]
    if not isinstance(target, str):
        return target
    out = []
    for item in target.split(","):
        stripped = item.strip()
        name, sep, chat = stripped.partition(":")
        if name == OLD_PLATFORM:
            stripped = NEW_PLATFORM + sep + str(_chat(chat)) if sep else NEW_PLATFORM
            item = item.replace(item.strip(), stripped)
        out.append(item)
    return ",".join(out)


def _json_rewrite(raw: Any, fn) -> tuple[Any, bool]:
    if not raw:
        return raw, False
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    if not fn(doc):
        return raw, False
    return json.dumps(doc, ensure_ascii=False), True


# ── state.db ──────────────────────────────────────────────────────────────


def _tables(conn) -> dict[str, set[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: {r[1] for r in conn.execute(f'PRAGMA table_info("{n}")')} for n in names}


_LIKE = f"%:{OLD_PLATFORM}:%"


def _plan_state(conn) -> dict[str, list]:
    """Every row to change, by table, as (rowid, new values...). Raises
    RouteCollision when a renamed key already exists. Read-only."""
    tables = _tables(conn)
    plan: dict[str, list] = {}

    def keyed(table: str, extra_cols: str = "") -> list:
        if table not in tables:
            return []
        cols = f"rowid, session_key{extra_cols}"
        return conn.execute(
            f"SELECT {cols} FROM {table} WHERE session_key LIKE ?", (_LIKE,)
        ).fetchall()

    routing = []
    for rowid, key, scope, entry_json in keyed("gateway_routing", ", scope, entry_json"):
        new_key = rekey(key)
        if new_key is None:
            continue
        hit = conn.execute(
            "SELECT 1 FROM gateway_routing WHERE scope = ? AND session_key = ?", (scope, new_key)
        ).fetchone()
        if hit:
            raise RouteCollision(f"gateway_routing already holds {new_key!r} (scope {scope!r})")
        new_json, _ = _json_rewrite(entry_json, rewrite_entry)
        routing.append((rowid, new_key, new_json))
    plan["gateway_routing"] = routing

    sessions = []
    if "sessions" in tables:
        cols = tables["sessions"]
        handoff = "handoff_platform" if "handoff_platform" in cols else "NULL"
        rows = conn.execute(
            "SELECT rowid, source, session_key, origin_json, chat_id, display_name, "
            f"{handoff} FROM sessions WHERE source = ? OR session_key LIKE ? "
            f"OR origin_json LIKE ? OR {handoff} = ?",
            (OLD_PLATFORM, _LIKE, f'%"{OLD_PLATFORM}"%', OLD_PLATFORM),
        ).fetchall()
        for rowid, source, key, origin_json, chat_id, display, hp in rows:
            new = (
                NEW_PLATFORM if source == OLD_PLATFORM else source,
                rekey(key) or key,
                _json_rewrite(origin_json, rewrite_origin)[0],
                _chat(chat_id) if source == OLD_PLATFORM else chat_id,
                _chat(display) if source == OLD_PLATFORM else display,
                NEW_PLATFORM if hp == OLD_PLATFORM else hp,
            )
            if new != (source, key, origin_json, chat_id, display, hp):
                sessions.append((rowid, *new))
    plan["sessions"] = sessions

    obligations = []
    if "delivery_obligations" in tables:
        rows = conn.execute(
            "SELECT rowid, platform, session_key, chat_id FROM delivery_obligations "
            "WHERE platform = ? OR session_key LIKE ?",
            (OLD_PLATFORM, _LIKE),
        ).fetchall()
        for rowid, platform, key, chat_id in rows:
            ours = platform == OLD_PLATFORM
            new = (
                NEW_PLATFORM if ours else platform,
                rekey(key) or key,
                _chat(chat_id) if ours else chat_id,
            )
            if new != (platform, key, chat_id):
                obligations.append((rowid, *new))
    plan["delivery_obligations"] = obligations

    # Bookkeeping keyed on the session key alone (or source + key). A target
    # that already exists is a collision, refused like a routing one.
    for table, pk_extra in (("gateway_hygiene_state", ""), ("conversation_generations", "source")):
        moved = []
        extra = f", {pk_extra}" if pk_extra else ""
        for row in keyed(table, extra):
            rowid, key = row[0], row[1]
            new_key = rekey(key)
            if new_key is None:
                continue
            new_source = None
            if pk_extra:
                new_source = NEW_PLATFORM if row[2] == OLD_PLATFORM else row[2]
                hit = conn.execute(
                    f"SELECT 1 FROM {table} WHERE source = ? AND session_key = ?",
                    (new_source, new_key),
                ).fetchone()
            else:
                hit = conn.execute(
                    f"SELECT 1 FROM {table} WHERE session_key = ?", (new_key,)
                ).fetchone()
            if hit:
                raise RouteCollision(f"{table} already holds {new_key!r}")
            moved.append((rowid, new_key, new_source))
        plan[table] = moved

    delegations = []
    if "async_delegations" in tables:
        for rowid, origin in conn.execute(
            "SELECT rowid, origin_session FROM async_delegations WHERE origin_session LIKE ?",
            (_LIKE,),
        ).fetchall():
            new_key = rekey(origin)
            if new_key is not None:
                delegations.append((rowid, new_key))
    plan["async_delegations"] = delegations
    return plan


def _apply_state(conn, plan: dict[str, list]) -> None:
    handoff = "handoff_platform" in _tables(conn).get("sessions", set())
    for rowid, key, entry_json in plan["gateway_routing"]:
        conn.execute(
            "UPDATE gateway_routing SET session_key = ?, entry_json = ? WHERE rowid = ?",
            (key, entry_json, rowid),
        )
    for rowid, source, key, origin_json, chat_id, display, hp in plan["sessions"]:
        sets = "source = ?, session_key = ?, origin_json = ?, chat_id = ?, display_name = ?"
        args = [source, key, origin_json, chat_id, display]
        if handoff:
            sets += ", handoff_platform = ?"
            args.append(hp)
        conn.execute(f"UPDATE sessions SET {sets} WHERE rowid = ?", (*args, rowid))
    for rowid, platform, key, chat_id in plan["delivery_obligations"]:
        conn.execute(
            "UPDATE delivery_obligations SET platform = ?, session_key = ?, chat_id = ? "
            "WHERE rowid = ?",
            (platform, key, chat_id, rowid),
        )
    for rowid, key, _ in plan["gateway_hygiene_state"]:
        conn.execute(
            "UPDATE gateway_hygiene_state SET session_key = ? WHERE rowid = ?", (key, rowid)
        )
    for rowid, key, source in plan["conversation_generations"]:
        conn.execute(
            "UPDATE conversation_generations SET source = ?, session_key = ? WHERE rowid = ?",
            (source, key, rowid),
        )
    for rowid, key in plan["async_delegations"]:
        conn.execute(
            "UPDATE async_delegations SET origin_session = ? WHERE rowid = ?", (key, rowid)
        )


def _counts(plan: dict[str, list]) -> dict[str, int]:
    return {table: len(rows) for table, rows in plan.items() if rows}


def migrate_state_db(db, *, dry_run: bool = False) -> dict[str, int]:
    """Rewrite one ``hermes_state.SessionDB``. All or nothing (one
    ``_execute_write``); ``dry_run`` only counts. Returns rows changed per table."""
    if dry_run:
        # SessionDB's own connection (read-only when opened ``read_only=True``).
        return _counts(_plan_state(db._conn))

    def _do(conn):
        plan = _plan_state(conn)
        _apply_state(conn, plan)
        return _counts(plan)

    # _do is idempotent under _execute_write's whole-callback retry: it re-plans.
    return db._execute_write(_do)


# ── the files beside it ───────────────────────────────────────────────────


def _staged(path: Path, data: Any) -> Path:
    """Write ``data`` beside ``path`` under a temporary name, with ``path``'s
    mode. The caller ``os.replace``s it into place (or unlinks it), so every
    byte is on disk before state.db commits and the rename after it cannot run
    out of space."""
    tmp = path.with_name(f".{path.name}.burrow-{os.getpid()}")
    mode = path.stat().st_mode & 0o777
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    return tmp


def plan_sessions_json(path: Path) -> tuple[dict | None, int]:
    """(rewritten mapping, keys moved) for the legacy routing mirror. Raises
    RouteCollision when a renamed key is already in the file."""
    if not path.exists():
        return None, 0
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return None, 0
    out: dict[str, Any] = {}
    moved = 0
    for key, value in data.items():
        new_key = rekey(key)
        if new_key is None:
            out[key] = value
            continue
        if new_key in data:
            raise RouteCollision(f"{path} already holds {new_key!r}")
        rewrite_entry(value)
        out[new_key] = value
        moved += 1
    return (out if moved else None), moved


def plan_cron_jobs(path: Path) -> tuple[Any, int]:
    """(rewritten document, jobs changed) for cron/jobs.json."""
    if not path.exists():
        return None, 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(jobs, list):
        return None, 0
    changed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        before = json.dumps(job, sort_keys=True)
        for field in ("deliver", "failure_deliver"):
            if field in job:
                job[field] = rewrite_target(job[field])
        rewrite_origin(job.get("origin"))
        if json.dumps(job, sort_keys=True) != before:
            changed += 1
    return (doc if changed else None), changed


# ── the home ──────────────────────────────────────────────────────────────


def gateway_running(home: Path) -> bool:
    """Hermes's own answer, scoped to this home's identity files, and never
    unlinking them (``cleanup_stale=False``)."""
    from gateway.status import is_gateway_running

    return bool(is_gateway_running(home / "gateway.pid", cleanup_stale=False))


def migrate_home(home: str | os.PathLike, *, dry_run: bool = False) -> dict[str, Any]:
    """Move one Hermes home. Returns ``{"moved": {place: count}, ...}``.

    Every place is planned (and every collision found) before anything is
    written. state.db is one transaction; the two JSON files are each
    replaced atomically after it commits. Refuses with GatewayRunning when a
    gateway is up for this home and anything is to move."""
    home = Path(home)
    from hermes_state import SessionDB

    db_path = home / "state.db"
    sessions_json = home / "sessions" / "sessions.json"
    jobs_json = home / "cron" / "jobs.json"

    db = SessionDB(db_path, read_only=True) if db_path.exists() else None
    try:
        pending = migrate_state_db(db, dry_run=True) if db is not None else {}
    finally:
        if db is not None:
            db.close()
    mirror, mirror_moved = plan_sessions_json(sessions_json)
    jobs, jobs_moved = plan_cron_jobs(jobs_json)
    if mirror_moved:
        pending["sessions.json"] = mirror_moved
    if jobs_moved:
        pending["cron/jobs.json"] = jobs_moved

    result: dict[str, Any] = {"from": OLD_PLATFORM, "to": NEW_PLATFORM, "home": str(home)}
    if not pending:
        return {**result, "moved": {}}
    if dry_run:
        return {**result, "pending": pending, "moved": {}}
    if gateway_running(home):
        raise GatewayRunning(
            f"a Hermes gateway is running for {home}; it holds the routing index in memory "
            "and would write the old keys back. Stop it, then run this again "
            f"(pending: {json.dumps(pending, sort_keys=True)})"
        )

    staged: list[tuple[Path, Path]] = []
    try:
        if mirror is not None:
            staged.append((_staged(sessions_json, mirror), sessions_json))
        if jobs is not None:
            staged.append((_staged(jobs_json, jobs), jobs_json))
        moved: dict[str, int] = {}
        if db_path.exists():
            db = SessionDB(db_path)
            try:
                moved.update(migrate_state_db(db))
            finally:
                db.close()
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, path in staged:
        os.replace(tmp, path)
    if mirror is not None:
        moved["sessions.json"] = mirror_moved
    if jobs is not None:
        moved["cron/jobs.json"] = jobs_moved
    return {**result, "moved": moved}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_burrow_channel.platform_rename")
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--check", action="store_true", help="count what would move; write nothing")
    args = parser.parse_args(argv)
    home = Path(args.hermes_home).expanduser().resolve()
    env_home = os.environ.get("HERMES_HOME")
    if env_home and Path(env_home).expanduser().resolve() != home:
        print(json.dumps({"ok": False, "error": f"HERMES_HOME={env_home} is not {home}"}))
        return 1
    os.environ["HERMES_HOME"] = str(home)

    def say(doc: dict[str, Any]) -> None:
        print(json.dumps({"tool": RESULT_TAG, **doc}, sort_keys=True))

    try:
        result = migrate_home(home, dry_run=args.check)
    except (GatewayRunning, RouteCollision) as exc:
        say({"ok": False, "refused": type(exc).__name__, "error": str(exc)})
        return 2
    except ImportError as exc:
        say(
            {
                "ok": False,
                "error": f"Hermes modules are not importable ({exc}); run this "
                "with Hermes's interpreter and its modules on PYTHONPATH",
            }
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - one JSON line, whatever broke
        say({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    say({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
