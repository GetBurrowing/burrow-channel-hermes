"""Token hygiene. The sentinel must appear in NO captured log record across
the connect, join-failure, reconnect and push-timeout paths.

Positive controls: the test also asserts the records it scans include the
lines that WOULD carry the token (the websockets DEBUG request line and our
own "connecting to" line), so an empty or unrelated capture cannot pass it.
"""

import logging

import pytest
from conftest import make_config

from hermes_suite_channel.redact import REDACTED, RedactingLogger, redact
from hermes_suite_channel.suite_client import PushTimeout, SuiteClient

SENTINEL = "tok-SENTINEL-01a0d6c5"


def test_redact_strips_query_strings_and_secrets():
    url = f"wss://h.example/runtime/ws/websocket?vsn=2.0.0&runtime_id=r&token={SENTINEL}"
    assert redact(url) == f"wss://h.example/runtime/ws/websocket?{REDACTED}"
    line = f"> GET /runtime/ws/websocket?vsn=2.0.0&token={SENTINEL} HTTP/1.1"
    assert SENTINEL not in redact(line)
    assert redact(f"bare {SENTINEL} value", [SENTINEL]) == f"bare {REDACTED} value"
    assert redact("tok a+b/c", ["a+b/c"]) == f"tok {REDACTED}"
    assert redact("q a%2Bb%2Fc", ["a+b/c"]) == f"q {REDACTED}"


def test_redacting_logger_formats_args_and_tracebacks(caplog):
    caplog.set_level(logging.DEBUG)
    log = RedactingLogger(logging.getLogger("test.redact"), [SENTINEL])
    log.info("dialing %s", f"ws://h/x?token={SENTINEL}")
    try:
        raise ValueError(f"bad {SENTINEL}")
    except ValueError:
        log.error("boom", exc_info=True)
    text = "\n".join(r.getMessage() + str(r.exc_text or "") for r in caplog.records)
    assert "ValueError" in text
    assert SENTINEL not in text


async def test_sentinel_token_never_reaches_a_log_record(server, caplog):
    caplog.set_level(logging.DEBUG)
    server.reject_handshakes = 1  # connect path: HTTP 403 on the upgrade
    server.refuse_joins = 1  # join-failure path
    server.replies["reply"] = None  # push-timeout path

    client = SuiteClient(make_config(server.url, token=SENTINEL, push_timeout_ms=100))
    await client.start()
    try:
        await client.wait_joined(5)  # after two reconnects
        with pytest.raises(PushTimeout) as info:
            await client.send_reply("sp-1", "hi")
        assert SENTINEL not in str(info.value)
    finally:
        await client.stop()

    # The fake really received the sentinel, so it was on the wire.
    assert any(SENTINEL in p for p in server.request_paths)

    records = [r.getMessage() + "\n" + str(r.exc_text or "") for r in caplog.records]
    joined = "\n".join(records)
    # Positive controls: each path produced its log line.
    assert "> GET /runtime/ws/websocket?" in joined  # websockets DEBUG request line
    assert "connecting to ws://127.0.0.1" in joined
    assert "InvalidStatus" in joined  # the 403
    assert "failed to join runtime:rt-01a0d6c5" in joined
    assert "reconnect scheduled" in joined
    assert "no ack after 100ms" in joined

    leaks = [
        (r.name, r.getMessage())
        for r in caplog.records
        if SENTINEL in r.getMessage() + str(r.exc_text or "")
    ]
    assert leaks == []
