"""The v0.5.0 platform-key move (``startup_suite`` -> ``burrow``), against the
REAL Hermes session store: routes are created by ``SessionStore`` under the old
key, moved by ``platform_rename``, and then resolved by a fresh
``SessionStore`` under the new key, which must hand back the SAME session.

``test_control_*`` shows the guard firing: without the move, the same lookup
starts a fresh session, which is the orphaning this module exists to prevent.
"""

from __future__ import annotations

import json
import shutil

import pytest

try:
    from gateway.config import GatewayConfig, Platform
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.session import SessionSource, SessionStore
    from hermes_state import SessionDB
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError("set HERMES_AGENT_SRC to a hermes-agent checkout") from exc

from hermes_burrow_channel import adapter, platform_rename
from hermes_burrow_channel.platform_rename import (
    NEW_PLATFORM,
    OLD_PLATFORM,
    GatewayRunning,
    RouteCollision,
    migrate_home,
    rekey,
    rewrite_target,
)

DM = "01a11822-e498-7001-9d40-382ab166116c"
SPACE = "01a0dbce-93f7-7001-b712-da5cc4f96e3c"


def test_the_names():
    assert (OLD_PLATFORM, NEW_PLATFORM) == ("startup_suite", "burrow")
    assert adapter.PLATFORM_NAME == NEW_PLATFORM
    assert platform_rename.CHAT_ID_RENAMES == {"suite:no-space": adapter.NO_SPACE_CHAT}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (f"agent:main:startup_suite:dm:{DM}", f"agent:main:burrow:dm:{DM}"),
        (
            f"agent:main:startup_suite:group:{SPACE}:Ryan Milvenan",
            f"agent:main:burrow:group:{SPACE}:Ryan Milvenan",
        ),
        (
            "agent:main:startup_suite:group:suite:no-space:suite-system",
            "agent:main:burrow:group:burrow:no-space:suite-system",
        ),
        ("agent:coder:startup_suite:dm", "agent:coder:burrow:dm"),
        ("agent:main:startup_suite", "agent:main:burrow"),
    ],
)
def test_rekey_moves_exactly_the_platform_segment(old, new):
    assert rekey(old) == new
    assert rekey(new) is None  # idempotent


@pytest.mark.parametrize(
    "key",
    [
        f"agent:main:telegram:dm:{DM}",
        "agent:main:telegram:group:startup_suite:x",  # the word in a chat id is not the platform
        "agent:main:startup_suite_x:dm:1",
        "startup_suite:dm:1",
        None,
        7,
    ],
)
def test_rekey_leaves_everything_else(key):
    assert rekey(key) is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("startup_suite", "burrow"),
        (f"startup_suite:{DM}", f"burrow:{DM}"),
        ("startup_suite:suite:no-space", "burrow:burrow:no-space"),
        (f"telegram:1, startup_suite:{DM}", f"telegram:1, burrow:{DM}"),
        ("origin", "origin"),
        (["startup_suite", "local"], ["burrow", "local"]),
        (None, None),
    ],
)
def test_cron_targets(old, new):
    assert rewrite_target(old) == new


# ── against the real store ────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (OLD_PLATFORM, NEW_PLATFORM):
        if not platform_registry.is_registered(name):
            platform_registry.register(
                PlatformEntry(
                    name=name, label=name, adapter_factory=lambda c: None, check_fn=lambda: True
                )
            )
    yield home
    import hermes_state_registry

    hermes_state_registry.close_all()


def _store(home) -> SessionStore:
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)
    store = SessionStore(sessions, GatewayConfig(sessions_dir=sessions, write_sessions_json=True))
    store._ensure_loaded()
    return store


def _sources(platform: str) -> dict[str, SessionSource]:
    p = Platform(platform)
    no_space = "suite:no-space" if platform == OLD_PLATFORM else adapter.NO_SPACE_CHAT
    return {
        "dm": SessionSource(
            platform=p, chat_id=DM, chat_type="dm", user_id="u-ryan", user_name="Ryan"
        ),
        "group": SessionSource(
            platform=p,
            chat_id=SPACE,
            chat_name="General",
            chat_type="group",
            user_id="Ryan Milvenan",
            user_name="Ryan Milvenan",
        ),
        "no_space": SessionSource(
            platform=p,
            chat_id=no_space,
            chat_name=no_space,
            chat_type="group",
            user_id=adapter.SYSTEM_USER,
            user_name="Burrow",
        ),
    }


def _old_home(home) -> dict[str, str]:
    """A home as a pre-v0.5.0 gateway leaves it: routes, session rows, the
    sessions.json mirror and a cron job, all keyed ``startup_suite``."""
    store = _store(home)
    ids = {
        name: store.get_or_create_session(src).session_id
        for name, src in _sources(OLD_PLATFORM).items()
    }
    store._save()
    (home / "cron").mkdir(exist_ok=True)
    (home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "deliver": f"startup_suite:{DM}",
                        "origin": {"platform": OLD_PLATFORM, "chat_id": DM},
                    },
                    {"id": "j2", "deliver": "local"},
                ]
            }
        )
    )
    return ids


def _keys(home) -> list[str]:
    db = SessionDB(home / "state.db", read_only=True)
    try:
        return sorted(r[0] for r in db._conn.execute("SELECT session_key FROM gateway_routing"))
    finally:
        db.close()


def test_routes_carry_over_to_the_new_platform_key(home):
    ids = _old_home(home)
    assert all(":startup_suite:" in k for k in _keys(home))

    result = migrate_home(home)

    moved = result["moved"]
    assert moved["gateway_routing"] == 3
    assert moved["sessions"] == 3
    assert moved["sessions.json"] == 3
    assert moved["cron/jobs.json"] == 1
    assert all(k.startswith("agent:main:burrow:") for k in _keys(home))
    # A fresh store, as the next gateway start builds it, resolves each chat
    # under the NEW platform to the session it had before.
    store = _store(home)
    for name, src in _sources(NEW_PLATFORM).items():
        assert store.get_or_create_session(src).session_id == ids[name], name
    # The legacy mirror holds no old key, so the next load cannot re-import one.
    mirror = json.loads((home / "sessions" / "sessions.json").read_text())
    assert not [k for k in mirror if OLD_PLATFORM in k]
    # Durable rows: the channel directory and /resume list by `source`.
    db = SessionDB(home / "state.db", read_only=True)
    try:
        assert {
            r["session_id"] if "session_id" in r.keys() else r["id"]
            for r in db.list_gateway_sessions(platform=NEW_PLATFORM, active_only=False)
        } == set(ids.values())
        assert db.list_gateway_sessions(platform=OLD_PLATFORM, active_only=False) == []
    finally:
        db.close()
    jobs = json.loads((home / "cron" / "jobs.json").read_text())["jobs"]
    assert jobs[0]["deliver"] == f"burrow:{DM}" and jobs[0]["origin"]["platform"] == "burrow"
    assert jobs[1]["deliver"] == "local"


def test_control_without_the_move_every_chat_starts_a_fresh_session(home, tmp_path):
    """The guard firing: the same lookup on an unmigrated copy of the home
    gets a NEW session id for every chat."""
    ids = _old_home(home)
    import hermes_state_registry

    hermes_state_registry.close_all()
    copy = tmp_path / "unmigrated"
    shutil.copytree(home, copy)
    store = _store(copy)
    for name, src in _sources(NEW_PLATFORM).items():
        assert store.get_or_create_session(src).session_id != ids[name], name


def test_a_second_run_moves_nothing(home):
    _old_home(home)
    assert migrate_home(home)["moved"]
    before = (home / "sessions" / "sessions.json").read_bytes()
    assert migrate_home(home)["moved"] == {}
    assert (home / "sessions" / "sessions.json").read_bytes() == before


def test_check_counts_and_writes_nothing(home):
    _old_home(home)
    before = _keys(home)
    result = migrate_home(home, dry_run=True)
    assert result["pending"]["gateway_routing"] == 3 and result["moved"] == {}
    assert _keys(home) == before


def test_an_empty_home_has_nothing_to_move(home):
    assert migrate_home(home)["moved"] == {}


def test_a_collision_refuses_and_changes_nothing(home):
    _old_home(home)
    db = SessionDB(home / "state.db")
    try:
        scope = db._conn.execute("SELECT scope FROM gateway_routing LIMIT 1").fetchone()[0]
        db.save_gateway_routing_entry(f"agent:main:burrow:dm:{DM}", "{}", scope=scope)
    finally:
        db.close()
    before = _keys(home)
    with pytest.raises(RouteCollision, match="burrow:dm"):
        migrate_home(home)
    assert _keys(home) == before
    assert any(
        ":startup_suite:" in k
        for k in json.loads((home / "sessions" / "sessions.json").read_text())
    )


def test_a_running_gateway_refuses_and_changes_nothing(home, monkeypatch):
    _old_home(home)
    assert platform_rename.gateway_running(home) is False  # Hermes's own answer, no gateway here
    monkeypatch.setattr(platform_rename, "gateway_running", lambda _home: True)
    before = _keys(home)
    with pytest.raises(GatewayRunning, match="Stop it"):
        migrate_home(home)
    assert _keys(home) == before


def test_a_real_running_gateway_refuses_and_changes_nothing(home):
    """No monkeypatch: a child holds this home's gateway lock through
    Hermes's own writers (tests/test_gateway_running.py)."""
    from test_gateway_running import _start, _stop

    _old_home(home)
    before = _keys(home)
    child = _start(home)
    try:
        with pytest.raises(GatewayRunning, match="Stop it"):
            migrate_home(home)
    finally:
        _stop(child)
    assert _keys(home) == before
    assert migrate_home(home)["moved"]  # and moves once it is gone


def test_a_running_gateway_with_nothing_to_move_is_fine(home, monkeypatch):
    monkeypatch.setattr(platform_rename, "gateway_running", lambda _home: True)
    assert migrate_home(home)["moved"] == {}


def test_main_prints_one_json_line(home, capsys, monkeypatch):
    _old_home(home)
    assert platform_rename.main(["--hermes-home", str(home), "--check"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["pending"]["gateway_routing"] == 3
    assert platform_rename.main(["--hermes-home", str(home)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["moved"]["gateway_routing"] == 3
    monkeypatch.setattr(platform_rename, "gateway_running", lambda _home: True)
    assert platform_rename.main(["--hermes-home", str(home)]) == 0  # nothing left to move
    capsys.readouterr()


def test_main_refuses_with_exit_2(home, capsys, monkeypatch):
    _old_home(home)
    monkeypatch.setattr(platform_rename, "gateway_running", lambda _home: True)
    assert platform_rename.main(["--hermes-home", str(home)]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc == {**doc, "ok": False, "refused": "GatewayRunning"}


def test_main_refuses_a_different_hermes_home(home, tmp_path, capsys):
    assert platform_rename.main(["--hermes-home", str(tmp_path / "other")]) == 1
    assert "is not" in json.loads(capsys.readouterr().out)["error"]


def test_a_failed_state_db_write_leaves_the_files_and_no_temporaries(home, monkeypatch):
    _old_home(home)
    files = [home / "sessions" / "sessions.json", home / "cron" / "jobs.json"]
    before = [f.read_bytes() for f in files]
    real = platform_rename.migrate_state_db

    def boom(db, *, dry_run=False):
        if dry_run:
            return real(db, dry_run=True)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(platform_rename, "migrate_state_db", boom)
    with pytest.raises(RuntimeError, match="disk I/O"):
        migrate_home(home)
    assert [f.read_bytes() for f in files] == before
    assert not [p for f in files for p in f.parent.iterdir() if ".burrow-" in p.name]
    assert all(":startup_suite:" in k for k in _keys(home))


def test_the_rewritten_files_keep_their_mode(home):
    _old_home(home)
    jobs = home / "cron" / "jobs.json"
    jobs.chmod(0o600)
    migrate_home(home)
    assert jobs.stat().st_mode & 0o777 == 0o600
