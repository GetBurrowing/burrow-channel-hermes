"""SuiteAdapter against the REAL hermes-agent gateway and the fake Phoenix server.

``handle_message`` is stubbed (it spawns a Hermes agent turn); everything else
is Hermes's own code: BasePlatformAdapter, SendResult, MessageEvent,
PlatformEntry, and for progressive reveal the real GatewayStreamConsumer.
"""

import asyncio
import base64
import logging

import pytest
from conftest import FAST

try:
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
except ImportError as exc:  # pragma: no cover - a message, not a skip
    raise ImportError(
        "the adapter tests need hermes-agent's gateway package: set HERMES_AGENT_SRC to a "
        "hermes-agent checkout (see tests/conftest.py)"
    ) from exc

from hermes_suite_channel import adapter as adapter_mod
from hermes_suite_channel.adapter import (
    NO_SPACE_CHAT,
    PLATFORM_NAME,
    SYSTEM_USER,
    SuiteAdapter,
    register,
)

TOKEN = "tok-adapter-SENTINEL-01a0d6c5"
AT = chr(64)


class FakeCtx:
    """Stands in for Hermes's PluginContext; builds a REAL PlatformEntry, so an
    unknown kwarg raises TypeError exactly as ctx.register_platform would."""

    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
        entry = PlatformEntry(source="plugin", plugin_name="startup-suite-platform", **kwargs)
        platform_registry.register(entry)
        return entry


@pytest.fixture(autouse=True)
def hermes_env(monkeypatch, tmp_path):
    # Each test starts with no token held from an earlier test's register().
    monkeypatch.setattr(adapter_mod, "_ENV_TOKEN", None)
    monkeypatch.delenv("SUITE_RUNTIME_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("SUITE_RUNTIME_ID", "rt-01a0d6c5")
    monkeypatch.setenv("SUITE_RUNTIME_TOKEN", TOKEN)
    for key in list(__import__("os").environ):
        if key.startswith("SUITE_") and key.endswith("_MS"):
            monkeypatch.delenv(key)
    ctx = FakeCtx()
    register(ctx)
    return ctx


def make_adapter(url: str, **extra) -> SuiteAdapter:
    return SuiteAdapter(PlatformConfig(enabled=True, extra={"url": url, **FAST, **extra}))


@pytest.fixture
async def live(server, monkeypatch):
    monkeypatch.setenv("SUITE_URL", server.url)
    adapter = make_adapter(server.url)
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture
    assert await adapter.connect() is True
    adapter.events = events
    try:
        yield adapter
    finally:
        await adapter.disconnect()


def chat_attention(message_id="m-1", content="hello", **signal):
    return {
        "signal": {
            "reason": "mention",
            "space_id": "sp-1",
            "message_id": message_id,
            "agent_id": "agent-self",
            **signal,
        },
        "message": {"content": content, "author": "Ryan"},
    }


async def settle():
    await asyncio.sleep(0.1)


# ── registration ──────────────────────────────────────────────────────────


def test_register_uses_real_platform_entry_kwargs(hermes_env):
    kw = hermes_env.kwargs
    assert kw["name"] == "startup_suite"
    assert kw["required_env"] == ["SUITE_URL", "SUITE_RUNTIME_ID", "SUITE_RUNTIME_TOKEN_FILE"]
    assert kw["allowed_users_env"] == "SUITE_ALLOWED_USERS"
    assert kw["allow_all_env"] == "SUITE_ALLOW_ALL_USERS"
    for fn in ("check_fn", "validate_config", "is_connected", "env_enablement_fn"):
        assert callable(kw[fn])
    hint = kw["platform_hint"]
    assert "Startup Suite space" in hint and "markdown" in hint
    assert "mention" in hint and "add that agent to the space" in hint
    assert AT + "[" not in hint


def test_env_enablement_never_seeds_the_token(monkeypatch):
    monkeypatch.setenv("SUITE_URL", "wss://suite.example/runtime/ws")
    monkeypatch.setenv("SUITE_RECONNECT_MIN_MS", "1500")
    seed = adapter_mod._env_enablement()
    assert seed["url"] == "wss://suite.example/runtime/ws"
    assert seed["runtime_id"] == "rt-01a0d6c5"
    assert seed["reconnect_min_ms"] == 1500
    assert TOKEN not in repr(seed)
    cfg = PlatformConfig(enabled=True, extra=seed)
    adapter = SuiteAdapter(cfg)
    assert adapter.suite_config.token == TOKEN  # read from the secret store
    assert TOKEN not in repr(cfg.to_dict())  # and never lands in the serialised config


def test_missing_token_fails_connect_without_naming_values(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_ENV_TOKEN", None)
    adapter = make_adapter("wss://suite.example/runtime/ws")
    assert asyncio.run(adapter.connect()) is False
    assert adapter._fatal_error_code == "config_missing"
    assert "token" in adapter._fatal_error_message


# ── the token stays out of os.environ ─────────────────────────────────────


def test_plugin_load_takes_the_token_out_of_os_environ(hermes_env):
    """register() (plugin load) ran in the autouse fixture with the token in
    the environment. Every child Hermes spawns without a scrub inherits
    os.environ (tools/env_probe.py runs `pip --version` that way), so after
    load the variable must be gone."""
    import os

    assert "SUITE_RUNTIME_TOKEN" not in os.environ
    assert adapter_mod._ENV_TOKEN == TOKEN


async def test_client_still_connects_with_the_token_after_load(server, monkeypatch):
    import os

    monkeypatch.setenv("SUITE_URL", server.url)
    assert "SUITE_RUNTIME_TOKEN" not in os.environ
    adapter = make_adapter(server.url)
    assert await adapter.connect() is True
    try:
        assert any(f"token={TOKEN}" in path for path in server.request_paths)
        assert "SUITE_RUNTIME_TOKEN" not in os.environ
    finally:
        await adapter.disconnect()


def _token_file(path, token, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(mode)
    return path


async def test_token_file_named_by_env_is_used_and_never_enters_the_env(
    server, monkeypatch, tmp_path
):
    import os

    file_token = "tok-file-SENTINEL-01a0d6c5"
    tf = _token_file(tmp_path / "tok", file_token)
    monkeypatch.setattr(adapter_mod, "_ENV_TOKEN", None)
    monkeypatch.setenv("SUITE_RUNTIME_TOKEN_FILE", str(tf))
    monkeypatch.setenv("SUITE_URL", server.url)
    adapter = make_adapter(server.url)
    assert adapter.suite_config.token == file_token
    assert await adapter.connect() is True
    try:
        assert any(f"token={file_token}" in path for path in server.request_paths)
        assert file_token not in "".join(os.environ.values())
    finally:
        await adapter.disconnect()


def test_default_token_file_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SUITE_URL", "wss://suite.example/runtime/ws")
    from hermes_suite_channel.config import TOKEN_FILE_RELPATH

    monkeypatch.setattr(adapter_mod, "_ENV_TOKEN", None)
    _token_file(tmp_path / "hermes" / TOKEN_FILE_RELPATH, "tok-default-01a0d6c5")
    adapter = make_adapter("wss://suite.example/runtime/ws")
    assert adapter.suite_config.token == "tok-default-01a0d6c5"
    assert adapter_mod.check_requirements() is True


def test_group_readable_token_file_is_refused_without_its_content(monkeypatch, tmp_path):
    monkeypatch.setenv("SUITE_URL", "wss://suite.example/runtime/ws")
    monkeypatch.setattr(adapter_mod, "_ENV_TOKEN", None)
    tf = _token_file(tmp_path / "tok", "tok-loose-01a0d6c5", mode=0o640)
    monkeypatch.setenv("SUITE_RUNTIME_TOKEN_FILE", str(tf))
    adapter = make_adapter("wss://suite.example/runtime/ws")
    assert adapter.suite_config is None
    assert "chmod 600" in adapter._config_error
    assert "tok-loose-01a0d6c5" not in adapter._config_error
    assert adapter_mod.check_requirements() is False


def test_env_token_is_not_borrowed_inside_a_secondary_profile_scope(monkeypatch):
    """The value taken out of os.environ is the LAUNCH profile's; a
    multiplexed secondary profile must never fall back to it."""
    monkeypatch.setattr(adapter_mod, "profile_scoped", lambda: True)
    adapter = make_adapter("wss://suite.example/runtime/ws")
    assert adapter.suite_config is None and "token" in adapter._config_error


# ── inbound ───────────────────────────────────────────────────────────────


async def test_attention_becomes_one_message_event(live, server):
    payload = chat_attention(task_id="t-01a0d6c5")
    await server.push("attention", payload)
    await settle()
    (event,) = live.events
    header = event.text.split("\n")[0]
    for key, value in [
        ("space_id", "sp-1"),
        ("message_id", "m-1"),
        ("task_id", "t-01a0d6c5"),
        ("reason", "mention"),
        ("author", "Ryan"),
    ]:
        assert f'{key}="{value}"' in header
    assert event.text.split("\n")[2] == "hello"
    assert event.source.chat_id == "sp-1"
    assert event.source.platform.value == PLATFORM_NAME
    assert event.message_id == "m-1"
    assert event.user_name == "Ryan"
    assert event.raw_message == payload
    assert event.allow_gateway_control is False
    assert event.metadata["suite_task_id"] == "t-01a0d6c5"


async def test_system_event_with_no_message_yields_the_prompt(live, server):
    await server.push(
        "attention",
        {
            "signal": {
                "reason": "system_event",
                "responsibility_key": "execution",
                "task_id": "t-2",
                "prompt": "Run stage 3.",
                "metadata": {"space_id": "sp-exec", "task_id": "t-2"},
            }
        },
    )
    await settle()
    (event,) = live.events
    assert event.text.split("\n")[2] == "Run stage 3."
    assert event.source.chat_id == "sp-exec"  # reply target from metadata
    assert event.user_id == SYSTEM_USER
    assert 'reason="system_event"' in event.text.split("\n")[0]


async def test_self_authored_messages_are_dropped(live, server):
    server.replies["reply"] = ("ok", {"message_id": "m-own", "space_id": "sp-1"})
    result = await live.send("sp-1", "I said this")
    assert result.success and result.message_id == "m-own"
    await server.push("attention", chat_attention(message_id="m-own", content="I said this"))
    echoed = chat_attention(message_id="m-echo", content="me again")
    echoed["message"]["author_id"] = "agent-self"
    await server.push("attention", echoed)
    await server.push("attention", chat_attention(message_id="m-other", content="someone else"))
    await settle()
    assert [e.message_id for e in live.events] == ["m-other"]


async def test_hermes_dedupe_outlives_the_client_window(server, monkeypatch):
    monkeypatch.setenv("SUITE_URL", server.url)
    adapter = make_adapter(server.url, dedupe_window_ms=20)
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture
    assert await adapter.connect()
    try:
        await server.push("attention", chat_attention())
        await asyncio.sleep(0.1)  # past the client's 20 ms window
        await server.push("attention", chat_attention())
        await settle()
        assert len(events) == 1
    finally:
        await adapter.disconnect()


async def test_content_less_attention_is_dropped(live, server):
    await server.push("attention", {"signal": {"reason": "system_event"}})
    await settle()
    assert live.events == []


async def test_meeting_transcripts_are_declined_not_acked(live, server):
    await server.push("meeting_transcript", {"segment_id": "seg-1", "room_name": "r", "text": "hi"})
    await settle()
    assert live.events == []
    assert server.events("meeting_transcript_ack") == []


# ── outbound ──────────────────────────────────────────────────────────────


async def test_send_is_an_acked_reply(live, server):
    server.replies["reply"] = ("ok", {"message_id": "msg-9", "space_id": "sp-1"})
    result = await live.send("sp-1", "**hi**")
    assert result.success and result.message_id == "msg-9"
    ((_, frame),) = server.events("reply")
    assert frame[4] == {"space_id": "sp-1", "content": "**hi**"}


@pytest.mark.parametrize("recoverable", [True, False])
async def test_refused_send_is_a_send_result_not_an_exception(live, server, recoverable):
    server.replies["reply"] = ("error", {"error": "not a participant", "recoverable": recoverable})
    result = await live.send("sp-1", "hi")
    assert not result.success
    assert result.error == "not a participant"
    assert result.retryable is recoverable


async def test_no_space_dispatch_refuses_to_reply(live, server):
    result = await live.send(NO_SPACE_CHAT, "hi")
    assert not result.success and "no space" in result.error
    assert server.events("reply") == []


async def test_typing_true_then_false(live, server):
    await live.send_typing("sp-1")
    await live.stop_typing("sp-1")
    await server.wait_for(lambda: len(server.events("typing")) == 2)
    assert [f[4] for _, f in server.events("typing")] == [
        {"space_id": "sp-1", "typing": True},
        {"space_id": "sp-1", "typing": False},
    ]


async def test_media_goes_out_as_reply_with_media(live, server, tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.7 small")
    result = await live.send_document("sp-1", str(doc), caption="the report")
    assert result.success, result.error
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n")
    assert (await live.send_image_file("sp-1", str(img))).success
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNGdata").decode()
    assert (await live.send_image("sp-1", data_url, caption="inline")).success
    frames = [f[4] for _, f in server.events("reply_with_media")]
    assert frames[0]["content"] == "the report"
    assert frames[0]["attachments"] == [
        {
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "data": base64.b64encode(b"%PDF-1.7 small").decode(),
        }
    ]
    assert frames[1]["attachments"][0]["content_type"] == "image/png"
    assert frames[2]["attachments"][0]["data"] == base64.b64encode(b"\x89PNGdata").decode()


async def test_media_over_10_mib_is_refused_with_a_send_result(live, server, tmp_path):
    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(10 * 1024 * 1024 + 1)
    result = await live.send_document("sp-1", str(big))
    assert not result.success
    assert result.error_kind == "too_long" and "10 MiB" in result.error
    # Core measures the BASE64 length against div(10 MiB * 4, 3), so exactly
    # 10 MiB is refused and 10 MiB - 1 byte is the largest file it accepts.
    exact = tmp_path / "exact.bin"
    with exact.open("wb") as fh:
        fh.truncate(10 * 1024 * 1024)
    assert not (await live.send_document("sp-1", str(exact))).success
    largest = tmp_path / "largest.bin"
    with largest.open("wb") as fh:
        fh.truncate(10 * 1024 * 1024 - 1)
    assert (await live.send_document("sp-1", str(largest))).success
    assert len(server.events("reply_with_media")) == 1


async def test_streamed_response_is_one_chunk_id_ending_done_true(live, server):
    """Drive Hermes's real GatewayStreamConsumer through the adapter."""
    consumer = GatewayStreamConsumer(
        live, "sp-1", StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1)
    )
    task = asyncio.create_task(consumer.run())
    for piece in ["Hello there, ", "this answer ", "is streamed ", "in pieces."]:
        consumer.on_delta(piece)
        await asyncio.sleep(0.05)
    final = "Hello there, this answer is streamed in pieces."
    consumer.finish(final)
    await asyncio.wait_for(task, 5)
    await server.wait_for(lambda: any(f[4]["done"] for _, f in server.events("reply_chunk")))

    chunks = [f[4] for _, f in server.events("reply_chunk")]
    assert len(chunks) >= 2
    assert {c["chunk_id"] for c in chunks} == {chunks[0]["chunk_id"]}  # exactly one chunk_id
    assert [c["done"] for c in chunks] == [False] * (len(chunks) - 1) + [True]
    assert chunks[-1]["text"] == final
    # Persisted once, before the bubble is removed.
    replies = server.events("reply")
    assert [f[4]["content"] for _, f in replies] == [final]
    order = [f[3] for _, f in server.frames if f[3] in ("reply", "reply_chunk")]
    assert order[-2:] == ["reply", "reply_chunk"]


async def test_non_streamed_send_is_a_plain_reply(live, server):
    consumer = GatewayStreamConsumer(live, "sp-1", StreamConsumerConfig(edit_interval=0.01))
    # The whole answer arrives before the first tick: the consumer's first
    # send is its finalize send, which carries no expect_edits.
    consumer.on_delta("short answer")
    consumer.finish("short answer")
    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(task, 5)
    await settle()
    assert [f[4]["content"] for _, f in server.events("reply")] == ["short answer"]
    assert server.events("reply_chunk") == []


async def test_finalized_stream_is_idempotent_and_never_reposts(live, server):
    opened = await live.send("sp-1", "draft", metadata={"expect_edits": True})
    handle = opened.message_id
    assert (await live.edit_message("sp-1", handle, "final", finalize=True)).success
    again = await live.edit_message("sp-1", handle, "final", finalize=True)
    assert again.success
    changed = await live.edit_message("sp-1", handle, "different", finalize=True)
    assert not changed.success
    assert [f[4]["content"] for _, f in server.events("reply")] == ["final"]


async def test_failed_finalize_leaves_the_stream_open(live, server):
    server.replies["reply"] = ("error", {"error": "db down", "recoverable": True})
    opened = await live.send("sp-1", "draft", metadata={"expect_edits": True})
    result = await live.edit_message("sp-1", opened.message_id, "final", finalize=True)
    assert not result.success and result.retryable
    await settle()
    assert all(not f[4]["done"] for _, f in server.events("reply_chunk"))


# ── in_reply_to_message_id (task 01a0d8de) ────────────────────────────────

SUITE_MSG = "01a0d8de-aaaa-7001-9246-3f8fe86630f6"


async def test_send_with_a_suite_reply_to_forwards_in_reply_to(live, server):
    server.replies["reply"] = ("ok", {"message_id": "msg-9", "space_id": "sp-1"})
    assert (await live.send("sp-1", "hi", reply_to=SUITE_MSG)).success
    ((_, frame),) = server.events("reply")
    assert frame[4] == {"space_id": "sp-1", "content": "hi", "in_reply_to_message_id": SUITE_MSG}


@pytest.mark.parametrize("reply_to", [None, "", "m-1", "chunk:" + SUITE_MSG, SUITE_MSG + "x", 7])
async def test_non_suite_reply_to_is_not_forwarded(live, server, reply_to):
    # The positive control for the case above: a send that always wrote the key
    # (or wrote whatever reply_to held) fails here.
    assert (await live.send("sp-1", "hi", reply_to=reply_to)).success
    ((_, frame),) = server.events("reply")
    assert frame[4] == {"space_id": "sp-1", "content": "hi"}


async def test_stream_finalise_reply_carries_the_reply_to_from_the_opening_send(live, server):
    opened = await live.send("sp-1", "draft", reply_to=SUITE_MSG, metadata={"expect_edits": True})
    assert (await live.edit_message("sp-1", opened.message_id, "more")).success
    assert (await live.edit_message("sp-1", opened.message_id, "final", finalize=True)).success
    await server.wait_for(lambda: any(f[4]["done"] for _, f in server.events("reply_chunk")))
    ((_, reply),) = server.events("reply")
    assert reply[4] == {"space_id": "sp-1", "content": "final", "in_reply_to_message_id": SUITE_MSG}
    # The ephemeral chunk pushes are unchanged: no annotation.
    for _, f in server.events("reply_chunk"):
        assert "in_reply_to_message_id" not in f[4]


async def test_real_stream_consumer_anchors_the_persisted_reply(live, server):
    """Hermes's own GatewayStreamConsumer, given the inbound message id as its
    reply anchor, ends in one persisted reply that carries it."""
    consumer = GatewayStreamConsumer(
        live,
        "sp-1",
        StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        initial_reply_to_id=SUITE_MSG,
    )
    task = asyncio.create_task(consumer.run())
    for piece in ["Hello there, ", "this answer ", "is streamed."]:
        consumer.on_delta(piece)
        await asyncio.sleep(0.05)
    final = "Hello there, this answer is streamed."
    consumer.finish(final)
    await asyncio.wait_for(task, 5)
    await server.wait_for(lambda: any(f[4]["done"] for _, f in server.events("reply_chunk")))
    replies = [f[4] for _, f in server.events("reply")]
    assert replies == [{"space_id": "sp-1", "content": final, "in_reply_to_message_id": SUITE_MSG}]


# ── token hygiene at the adapter boundary ─────────────────────────────────


async def test_malformed_suite_url_fails_connect_without_the_token(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("SUITE_URL", "ftp://suite.example/runtime/ws")
    adapter = make_adapter("ftp://suite.example/runtime/ws")
    assert await adapter.connect() is False
    assert adapter._fatal_error_code == "invalid_url"
    assert TOKEN not in adapter._fatal_error_message
    assert "SUITE_URL" in adapter._fatal_error_message
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


async def test_inbound_images_reach_media_fields(live, monkeypatch, tmp_path):
    from pathlib import Path

    from gateway.platforms.event import MessageType

    calls = []

    def download(att, url, token, cache, remaining):
        calls.append(att)
        path = tmp_path / (str(len(calls)) + ".png")
        path.write_bytes(b"image-test-fixture")
        return str(path), "image/png", 18

    monkeypatch.setattr(adapter_mod, "download_image", download)
    payload = chat_attention(content="look here")
    payload["message"]["attachments"] = [
        {"content_type": "image/png", "id": "one"},
        {"content_type": "image/png", "id": "two"},
    ]
    await live._on_attention(payload)
    event = live.events[-1]
    assert event.message_type == MessageType.PHOTO
    assert len(event.media_urls) == 2
    assert all(Path(p).is_file() for p in event.media_urls)
    assert event.media_types == ["image/png", "image/png"]
    assert "look here" in event.text and "suite-untrusted" in event.text
    assert event.allow_gateway_control is False
    await live._on_attention(payload)
    assert len(calls) == 2  # duplicate does not download again


async def test_image_only_and_partial_failure(live, monkeypatch):
    calls = []

    def download(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("secret must not enter event")
        return "/local/test.png", "image/png", 1

    monkeypatch.setattr(adapter_mod, "download_image", download)
    payload = chat_attention(content="")
    payload["message"]["attachments"] = [{"content_type": "image/png"}] * 2
    await live._on_attention(payload)
    event = live.events[-1]
    assert "[Image attachment]" in event.text
    assert "System dispatch" not in event.text
    assert event.media_urls == ["/local/test.png"]
    assert event.metadata["suite_image_failures"] == 1
    assert "secret" not in event.text


async def test_image_count_cap_and_all_failed_still_delivers_text(live, monkeypatch):
    calls = []

    def download(*args):
        calls.append(args)
        raise OSError("denied")

    monkeypatch.setattr(adapter_mod, "download_image", download)
    payload = chat_attention(content="caption survives")
    payload["message"]["attachments"] = [{"content_type": "image/png"}] * 10
    await live._on_attention(payload)
    event = live.events[-1]
    assert len(calls) == 8
    assert event.media_urls == []
    assert event.metadata["suite_image_failures"] == 10
    assert "caption survives" in event.text
