import asyncio
import base64
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_config

from hermes_suite_channel import __version__
from hermes_suite_channel.suite_client import (
    AttachmentTooLarge,
    NotConnected,
    PushRefused,
    PushTimeout,
    SuiteClient,
    attention_identity,
    socket_url,
)


async def started(server, **kwargs):
    handlers = {k: kwargs.pop(k) for k in list(kwargs) if k.startswith("on_")}
    client = SuiteClient(make_config(server.url, **kwargs), **handlers)
    await client.start()
    await client.wait_joined(3)
    return client


def attention(space="sp-1", message_id="m-1", content="hello"):
    return {
        "signal": {"reason": "mention", "space_id": space, "message_id": message_id},
        "message": {"content": content, "author": "Ryan"},
    }


async def test_join_topic_and_client_info(server):
    client = await started(server)
    try:
        ((_, join),) = server.events("phx_join")
        join_ref, ref, topic, _event, payload = join
        assert topic == "runtime:rt-01a0d6c5"
        assert join_ref == ref
        info = payload["client_info"]
        assert info["product"] == "hermes_channel"
        assert info["version"] == __version__
        assert isinstance(info["build"], str) and info["build"]
        assert info["features"] == ["system_event_prompt"]
        datetime.fromisoformat(info["started_at"])

        path = urlsplit(server.request_paths[0])
        assert path.path == "/runtime/ws/websocket"
        assert parse_qs(path.query) == {
            "vsn": ["2.0.0"],
            "runtime_id": ["rt-01a0d6c5"],
            "token": ["tok-plain-01a0d6c5"],
        }
    finally:
        await client.stop()


async def test_reply_resolves_on_ok(server):
    server.replies["reply"] = ("ok", {"message_id": "msg-1"})
    client = await started(server)
    try:
        assert await client.send_reply("sp-1", "hi") == {"message_id": "msg-1"}
        ((_, frame),) = server.events("reply")
        assert frame[2] == "runtime:rt-01a0d6c5"
        assert frame[4] == {"space_id": "sp-1", "content": "hi"}
    finally:
        await client.stop()


async def test_reply_extras_are_merged_and_none_values_dropped(server):
    server.replies["reply"] = ("ok", {"message_id": "msg-1"})
    client = await started(server)
    try:
        await client.send_reply("sp-1", "hi", {"in_reply_to_message_id": "m-9", "context": None})
        await client.send_reply("sp-1", "hi", None)
        with_extras, without = (f[4] for _, f in server.events("reply"))
        assert with_extras == {"space_id": "sp-1", "content": "hi", "in_reply_to_message_id": "m-9"}
        assert without == {"space_id": "sp-1", "content": "hi"}
    finally:
        await client.stop()


async def test_reply_extras_cannot_replace_space_id_or_content(server):
    server.replies["reply"] = ("ok", {"message_id": "msg-1"})
    client = await started(server)
    try:
        await client.send_reply("sp-1", "hi", {"space_id": "sp-evil", "content": "forged"})
        ((_, frame),) = server.events("reply")
        assert frame[4] == {"space_id": "sp-1", "content": "hi"}
    finally:
        await client.stop()


@pytest.mark.parametrize("recoverable", [True, False])
async def test_reply_raises_on_error_carrying_recoverable(server, recoverable):
    server.replies["reply"] = ("error", {"error": "not a participant", "recoverable": recoverable})
    client = await started(server)
    try:
        with pytest.raises(PushRefused) as info:
            await client.send_reply("sp-1", "hi")
        assert str(info.value) == "not a participant"
        assert info.value.recoverable is recoverable
    finally:
        await client.stop()


async def test_reply_raises_on_timeout(server):
    server.replies["reply"] = None
    client = await started(server, push_timeout_ms=100)
    try:
        loop = asyncio.get_running_loop()
        began = loop.time()
        with pytest.raises(PushTimeout) as info:
            await client.send_reply("sp-1", "hi")
        assert 0.09 <= loop.time() - began < 1.0
        assert info.value.recoverable is False
        assert info.value.outcome_unknown is True
    finally:
        await client.stop()


async def test_push_before_join_raises_not_connected(server):
    client = SuiteClient(make_config(server.url))
    with pytest.raises(NotConnected) as info:
        await client.send_reply("sp-1", "hi")
    assert info.value.recoverable is True


async def test_ping_gets_pong(server):
    client = await started(server)
    try:
        await server.push("ping", {"timestamp": "2026-09-25T00:00:00Z"})
        await server.wait_for(lambda: server.events("pong"))
        ((_, pong),) = server.events("pong")
        assert pong[2] == "runtime:rt-01a0d6c5"
        datetime.fromisoformat(pong[4]["timestamp"])
    finally:
        await client.stop()


async def test_phoenix_heartbeat_is_sent(server):
    client = await started(server, heartbeat_ms=50)
    try:
        await server.wait_for(lambda: len(server.events("heartbeat")) >= 2)
        _, beat = server.events("heartbeat")[0]
        assert beat[0] is None and beat[2] == "phoenix" and beat[4] == {}
        assert client.sessions == 1
    finally:
        await client.stop()


async def test_unacked_heartbeat_forces_reconnect(server):
    server.replies["heartbeat"] = None
    client = await started(server, heartbeat_ms=50)
    try:
        await server.wait_for(lambda: len(server.events("phx_join")) >= 2)
    finally:
        await client.stop()


async def test_reconnect_rejoins_and_delivers_next_attention_once(server):
    seen, joins = [], []
    client = await started(server, on_attention=seen.append, on_joined=lambda: joins.append(1))
    try:
        await server.close_current()
        await server.wait_for(lambda: len(server.events("phx_join")) == 2)
        await client.wait_joined(3)
        second_index, second = server.events("phx_join")[1]
        assert second_index == 1
        assert second[2] == "runtime:rt-01a0d6c5"
        assert second[4]["client_info"]["product"] == "hermes_channel"

        await server.push("attention", attention(message_id="after-reconnect"))
        await asyncio.sleep(0.2)
        assert [a["signal"]["message_id"] for a in seen] == ["after-reconnect"]
        assert client.sessions == 2
        assert len(joins) == 2
        assert len(server.connections) == 2
    finally:
        await client.stop()


async def test_duplicate_attention_inside_window_is_dropped(server):
    seen = []
    client = await started(server, on_attention=seen.append, dedupe_window_ms=300)
    try:
        await server.push("attention", attention())
        await server.push("attention", attention())
        await server.push("attention", attention(message_id="m-2"))
        await asyncio.sleep(0.1)
        assert [a["signal"]["message_id"] for a in seen] == ["m-1", "m-2"]

        await asyncio.sleep(0.35)  # past the window: the same identity is new again
        await server.push("attention", attention())
        await asyncio.sleep(0.1)
        assert [a["signal"]["message_id"] for a in seen] == ["m-1", "m-2", "m-1"]
    finally:
        await client.stop()


async def test_async_attention_handler_is_awaited(server):
    seen = []

    async def handler(payload):
        await asyncio.sleep(0)
        seen.append(payload)

    client = await started(server, on_attention=handler)
    try:
        await server.push("attention", attention())
        await asyncio.sleep(0.1)
        assert len(seen) == 1
    finally:
        await client.stop()


async def test_stale_traffic_watchdog_forces_reconnect(server):
    client = await started(server, watchdog_interval_ms=30, stale_traffic_ms=60)
    try:
        await server.wait_for(lambda: len(server.events("phx_join")) >= 2)
    finally:
        await client.stop()


async def test_phx_error_on_channel_forces_reconnect(server):
    client = await started(server)
    try:
        await server.push("phx_error", {})
        await server.wait_for(lambda: len(server.events("phx_join")) >= 2)
    finally:
        await client.stop()


async def test_refused_join_retries_with_backoff(server):
    server.refuse_joins = 2
    disconnects = []
    client = SuiteClient(make_config(server.url), on_disconnect=lambda: disconnects.append(1))
    await client.start()
    try:
        await client.wait_joined(3)
        assert len(server.events("phx_join")) == 3
        assert len(disconnects) == 2
    finally:
        await client.stop()


async def test_spaces_manifest_and_meeting_transcript_are_forwarded(server):
    manifests, frames = [], []
    client = await started(
        server, on_spaces_manifest=manifests.append, on_meeting_transcript=frames.append
    )
    try:
        await server.push("spaces_manifest", {"spaces": [{"id": "s", "name": "n", "kind": "k"}]})
        await server.push("meeting_transcript", {"segment_id": "seg", "text": "hi"})
        await asyncio.sleep(0.1)
        assert manifests == [[{"id": "s", "name": "n", "kind": "k"}]]
        assert frames == [{"segment_id": "seg", "text": "hi"}]
    finally:
        await client.stop()


async def test_reply_with_media_encodes_bytes(server):
    client = await started(server)
    try:
        await client.send_reply_with_media(
            "sp-1", "shot", [{"filename": "a.png", "content_type": "image/png", "data": b"\x89PNG"}]
        )
        ((_, frame),) = server.events("reply_with_media")
        assert frame[4]["attachments"] == [
            {
                "filename": "a.png",
                "content_type": "image/png",
                "data": base64.b64encode(b"\x89PNG").decode(),
            }
        ]
        big = b"\0" * (10 * 1024 * 1024 + 1)
        with pytest.raises(AttachmentTooLarge):
            await client.send_reply_with_media(
                "sp-1", "", [{"filename": "b", "content_type": "x/y", "data": big}]
            )
        assert len(server.events("reply_with_media")) == 1
    finally:
        await client.stop()


async def test_typing_and_chunk_refusals_are_recorded_then_cleared(server):
    server.replies["typing"] = ("error", {"error": "no access"})
    server.replies["reply_chunk"] = ("error", {"error": "chunk refused"})
    client = await started(server)
    try:
        client.send_typing("sp-1", True)
        client.send_reply_chunk("sp-1", "c1", "par", False)
        await asyncio.sleep(0.1)
        assert client.last_typing_refusal("sp-1") == "no access"
        assert client.last_chunk_refusal("sp-1") == "chunk refused"

        server.replies["typing"] = ("ok", {})
        client.send_typing("sp-1", False)
        await asyncio.sleep(0.1)
        assert client.last_typing_refusal("sp-1") is None
        assert client.last_chunk_refusal("sp-1") == "chunk refused"
    finally:
        await client.stop()


def test_backoff_doubles_to_the_cap_and_jitter_never_exceeds_it():
    cfg = make_config("ws://x", reconnect_min_ms=1000, reconnect_max_ms=30000)
    client = SuiteClient(cfg)
    assert [client.backoff_delay_ms(n) for n in range(7)] == [
        1000,
        2000,
        4000,
        8000,
        16000,
        30000,
        30000,
    ]
    jittered = SuiteClient(
        make_config("ws://x", reconnect_min_ms=1000, reconnect_max_ms=30000, reconnect_jitter=0.5),
        rng=lambda: 0.5,
    )
    assert jittered.backoff_delay_ms(10) == 22500
    for r in (0.0, 0.5, 0.999):
        capped = SuiteClient(make_config("ws://x", reconnect_jitter=1.0), rng=lambda r=r: r)
        assert 1 <= capped.backoff_delay_ms(9) <= 100


def test_socket_url_shape():
    cfg = make_config("https://suite.example/runtime/ws/", token="a b&c")
    url = socket_url(cfg)
    parts = urlsplit(url)
    assert (parts.scheme, parts.path) == ("wss", "/runtime/ws/websocket")
    assert parse_qs(parts.query)["token"] == ["a b&c"]


def test_attention_identity_mirrors_sibling():
    assert attention_identity(attention()) == "sp-1:m-1:::::hello"
    dispatch = {
        "signal": {
            "reason": "system_event",
            "responsibility_key": "execution",
            "prompt": "x" * 80,
            "metadata": {"task_id": "t", "plan_id": "p", "stage_id": "s1"},
        }
    }
    assert attention_identity(dispatch) == "::execution:t:p:s1:" + "x" * 50
    other_stage = {**dispatch, "signal": {**dispatch["signal"], "metadata": {"stage_id": "s2"}}}
    assert attention_identity(other_stage) != attention_identity(dispatch)
    # message.content wins over prompt even when empty, like JS `??`.
    both = {"signal": {"prompt": "p"}, "message": {"content": ""}}
    assert attention_identity(both).endswith(":")
