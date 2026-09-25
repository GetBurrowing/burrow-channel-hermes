import pytest
from fake_phoenix import FakePhoenix

from hermes_suite_channel.config import SuiteConfig

FAST = {
    "reconnect_min_ms": 20,
    "reconnect_max_ms": 100,
    "reconnect_jitter": 0.0,
    "push_timeout_ms": 200,
    "join_timeout_ms": 1000,
    "heartbeat_ms": 30_000,
    "watchdog_interval_ms": 30_000,
    "stale_traffic_ms": 120_000,
    "dedupe_window_ms": 1_000,
}


def make_config(url: str, token: str = "tok-plain-01a0d6c5", **overrides) -> SuiteConfig:
    return SuiteConfig(url=url, runtime_id="rt-01a0d6c5", token=token, **{**FAST, **overrides})


@pytest.fixture
async def server():
    async with FakePhoenix() as fake:
        yield fake
