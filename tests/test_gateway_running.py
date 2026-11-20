"""``platform_rename.gateway_running``: the vendored stand-in for Hermes's
removed ``gateway.status.is_gateway_running``.

The identity files are written by Hermes's OWN code (``acquire_gateway_runtime_lock``,
``write_pid_file``, ``write_runtime_status``) in a child process with
``HERMES_HOME`` set to the home, so the shape checked here is the shape a real
gateway leaves, not a fixture's guess at it. The no-side-effect claim has a
control: Hermes's own probe DOES delete the same stale files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from hermes_burrow_channel import platform_rename
from hermes_burrow_channel.platform_rename import gateway_running

try:
    from gateway import status as hermes_status
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError("set HERMES_AGENT_SRC to a hermes-agent checkout") from exc

HERMES_SRC = os.path.dirname(os.path.dirname(os.path.abspath(hermes_status.__file__)))

# A child that becomes "the gateway" for HERMES_HOME through Hermes's own
# writers, says READY, and holds the lock until stdin closes.
GATEWAY = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from gateway import status
    assert status.acquire_gateway_runtime_lock()
    status.write_pid_file()
    assert status.write_runtime_status(gateway_state=sys.argv[2], wait_timeout=10)
    print("READY", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    return home


def _start(home, state="running"):
    child = subprocess.Popen(
        [sys.executable, "-c", GATEWAY, HERMES_SRC, state],
        env={**os.environ, "HERMES_HOME": str(home)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "READY"
    return child


def _stop(child):
    child.stdin.close()
    child.wait(timeout=10)


def _snapshot(home):
    return {p.name: p.read_bytes() for p in sorted(home.iterdir())}


def test_an_empty_home_has_no_gateway(home):
    assert gateway_running(home) is False
    assert list(home.iterdir()) == []  # the probe created nothing


def test_a_live_gateway_is_running_and_its_files_are_untouched(home):
    child = _start(home)
    try:
        before = _snapshot(home)
        assert {"gateway.pid", "gateway.lock", "gateway_state.json"} <= set(before)
        assert gateway_running(home) is True
        assert _snapshot(home) == before
    finally:
        _stop(child)


def test_a_dead_gateway_is_not_running_and_its_stale_files_stay(home):
    _stop(_start(home))
    before = _snapshot(home)
    assert json.loads(before["gateway_state.json"])["gateway_state"] == "running"
    assert gateway_running(home) is False
    assert _snapshot(home) == before


def test_control_hermes_own_probe_deletes_those_stale_files(home):
    """What ``is_gateway_running`` defaulted to: the stale pid file and lock
    are unlinked. The test above therefore measures something."""
    _stop(_start(home))
    assert hermes_status.get_running_pid(home / "gateway.pid", cleanup_stale=True) is None
    assert not (home / "gateway.pid").exists() and not (home / "gateway.lock").exists()


def test_a_launch_service_gateway_without_its_pid_file_or_lock_is_running(home):
    """Hermes's live_gateway_pid_for_home falls back to gateway_state.json
    when gateway.pid was unlinked under a live gateway (#110166)."""
    child = _start(home)
    try:
        (home / "gateway.pid").unlink()
        (home / "gateway.lock").unlink()  # the child's flock now holds an orphaned inode
        assert gateway_running(home) is True
    finally:
        _stop(child)


@pytest.mark.parametrize("state", ["stopped", "startup_failed"])
def test_a_state_record_that_says_stopped_is_not_running(home, state):
    child = _start(home, state)
    try:
        (home / "gateway.lock").unlink()
        assert gateway_running(home) is False
    finally:
        _stop(child)


@pytest.fixture
def sleeper():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield child.pid
    _stop(child)


def _state(home, pid, **fields):
    record = {
        "pid": pid,
        "kind": "hermes-gateway",
        "argv": ["-c", "gateway", "run"],
        "start_time": platform_rename._start_time(pid),
        "hermes_home": str(home),
        "gateway_state": "running",
        **fields,
    }
    (home / "gateway_state.json").write_text(json.dumps(record))


def test_the_state_record_proves_a_live_pid(home, sleeper):
    _state(home, sleeper)
    assert gateway_running(home) is True  # positive control for the ones below


def test_a_recycled_pid_is_not_a_gateway(home, sleeper):
    if platform_rename._start_time(sleeper) is None:
        pytest.skip("no start-time source here (no /proc, no psutil)")
    _state(home, sleeper, start_time=platform_rename._start_time(sleeper) + 10_000)
    assert gateway_running(home) is False


def test_another_homes_record_is_not_this_homes_gateway(home, tmp_path, sleeper):
    _state(home, sleeper, hermes_home=str(tmp_path / "other"))
    assert gateway_running(home) is False


def test_this_process_is_not_a_gateway(home):
    """Hermes's SessionStore, opened in-process, stamps gateway_state.json
    with this process's PID as "starting" (tests/test_platform_rename.py's
    homes all carry one); the migration must not mistake itself for a
    gateway."""
    _state(home, os.getpid(), gateway_state="starting")
    assert gateway_running(home) is False


def test_a_gateway_in_this_process_still_counts(home):
    """...while a lock held by this very process is still seen: the probe's
    fresh open is a separate lock description."""
    import fcntl

    (home / "gateway.lock").write_text("{}")
    with open(home / "gateway.lock") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert gateway_running(home) is True
    assert gateway_running(home) is False


def test_an_unreadable_lock_is_left_in_place(home):
    """Hermes's lock probe unlinks a lock it cannot open; this one must not."""
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    lock = home / "gateway.lock"
    lock.write_text("{}")
    lock.chmod(0)
    try:
        assert gateway_running(home) is False
        assert lock.exists()
    finally:
        lock.chmod(0o600)
