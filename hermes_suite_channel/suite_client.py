"""Phoenix runtime-socket client for Startup Suite federation (asyncio).

A port of claude-code-suite-channel ``src/suite-client.ts`` to Python, using
the ``websockets`` library directly instead of the phoenix JS client.

Wire contract (core, origin/main):

- socket ``/runtime/ws`` (``PlatformWeb.Endpoint``), transport path
  ``/websocket``, serializer ``vsn=2.0.0``. ``PlatformWeb.RuntimeSocket``
  sets no ``serializer:`` option, so Phoenix 1.8's default pair (1.0.0 and
  2.0.0 JSON) applies.
- connect params ``runtime_id`` + ``token`` (``RuntimeSocket.connect/3``).
- V2 frames are JSON arrays ``[join_ref, ref, topic, event, payload]``.
- channel topic ``runtime:<runtime_id>``, join payload ``{client_info}``.
- inbound: ``attention``, ``ping`` (answered with ``pong``), ``spaces_manifest``,
  ``meeting_transcript``.
- outbound, acked: ``reply``, ``reply_with_media``; fire-and-forget with the
  refusal recorded: ``typing``, ``reply_chunk``.

Token hygiene: the token exists in exactly two places, ``SuiteConfig.token``
(``repr=False``) and the query string built by ``socket_url()``. Every log
record goes through ``RedactingLogger`` (hermes_suite_channel/redact.py),
including the websockets library's own DEBUG output, which logs the request
target and therefore the query string. No exception raised by this module
formats the URL or the config.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import itertools
import json
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from . import __version__
from ._build import BUILD
from .config import SuiteConfig
from .redact import RedactingLogger

PRODUCT = "hermes_channel"
PROTOCOL_VSN = "2.0.0"

# Declared capabilities; see the long comment on CLIENT_FEATURES in the
# sibling. Exact-match wire strings: never "tidy" one. `system_event_prompt`
# claims the adapter reads a responsibility dispatch's body from
# `signal.prompt` when the payload has no `message` key.
CLIENT_FEATURES: tuple[str, ...] = ("system_event_prompt",)

# Captured when the module is first imported. That is no earlier than the
# process start, so the one direction core relies on ("started_at earlier
# than a fix refutes freshness") stays sound: code imported before a fix
# existed cannot contain it.
PROCESS_STARTED_AT = datetime.now(UTC).isoformat()

# Incoming frame cap. An attention can carry a history snapshot; the default
# 1 MiB would drop the socket on a large one.
MAX_INCOMING_FRAME = 64 * 1024 * 1024

# Core runtime_channel.ex attachments_within_limits?/1: checked against the
# base64 length, 4/3 of the raw caps.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Sibling keeps the most recent 50 dedupe keys.
DEDUPE_CAPACITY = 50


def client_info() -> dict[str, Any]:
    return {
        "product": PRODUCT,
        "version": __version__,
        "build": BUILD,
        "started_at": PROCESS_STARTED_AT,
        "features": list(CLIENT_FEATURES),
    }


def socket_url(config: SuiteConfig) -> str:
    """The dial URL. Carries the token; never log it except through redact()."""
    base = config.url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    if not base.endswith("/websocket"):
        base += "/websocket"
    query = urlencode({"vsn": PROTOCOL_VSN, "runtime_id": config.runtime_id, "token": config.token})
    return f"{base}?{query}"


def attention_identity(payload: dict[str, Any]) -> str:
    """Dedupe identity, mirroring ``attentionIdentity()`` in the sibling.

    Every component can only make the key more unique. A byte-identical Suite
    retry still collapses; a responsibility dispatch (no space_id, no
    message_id) is told apart by its responsibility key and correlation ids.
    """
    signal = payload.get("signal")
    signal = signal if isinstance(signal, dict) else {}
    metadata = signal.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    message = payload.get("message")
    message = message if isinstance(message, dict) else {}

    def s(value: Any) -> str:
        return value if isinstance(value, str) else ""

    content = message.get("content")
    prompt = signal.get("prompt")
    if isinstance(content, str):
        head = content[:50]
    elif isinstance(prompt, str):
        head = prompt[:50]
    else:
        head = ""

    return ":".join(
        [
            s(signal.get("space_id")),
            s(signal.get("message_id")),
            s(signal.get("responsibility_key")),
            s(metadata.get("task_id")),
            s(metadata.get("plan_id")),
            s(metadata.get("stage_id")),
            head,
        ]
    )


class SuiteError(Exception):
    """Base for push failures. ``recoverable`` says whether a retry may succeed."""

    recoverable = False
    outcome_unknown = False

    def __init__(self, message: str, *, recoverable: bool | None = None):
        super().__init__(message)
        if recoverable is not None:
            self.recoverable = recoverable


class NotConnected(SuiteError):
    """The channel is not joined; nothing was sent."""

    recoverable = True


class PushRefused(SuiteError):
    """Suite answered the push with an error reply."""

    def __init__(self, message: str, *, recoverable: bool, response: dict[str, Any]):
        super().__init__(message, recoverable=recoverable)
        self.response = response


class PushTimeout(SuiteError):
    """No ack within the push timeout. The message MAY have landed, so a blind
    retry can duplicate it: not recoverable, outcome unknown."""

    outcome_unknown = True


class ConnectionLost(SuiteError):
    """The socket dropped with the push in flight. Outcome unknown."""

    outcome_unknown = True


class AttachmentTooLarge(SuiteError):
    """Rejected locally against core's attachment caps; nothing was sent."""


class InvalidSuiteURL(SuiteError):
    """SUITE_URL cannot be dialled as a websocket URI. Terminal: retrying the
    same URL cannot succeed, so the supervisor stops instead of looping."""


class _SessionEnded(Exception):
    """Internal: this socket session is over; the supervisor reconnects."""


Handler = Callable[..., Any] | None


class SuiteClient:
    def __init__(
        self,
        config: SuiteConfig,
        *,
        on_attention: Handler = None,
        on_joined: Handler = None,
        on_disconnect: Handler = None,
        on_spaces_manifest: Handler = None,
        on_meeting_transcript: Handler = None,
        on_fatal: Handler = None,
        rng: Callable[[], float] = random.random,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._topic = f"runtime:{config.runtime_id}"
        secrets = (config.token,)
        self._log = RedactingLogger(logger or logging.getLogger(__name__), secrets)
        self._ws_log = RedactingLogger(
            logging.getLogger("hermes_suite_channel.websockets"), secrets
        )
        self._handlers = {
            "attention": on_attention,
            "joined": on_joined,
            "disconnect": on_disconnect,
            "spaces_manifest": on_spaces_manifest,
            "meeting_transcript": on_meeting_transcript,
            "fatal": on_fatal,
        }
        self._rng = rng
        self._refs = itertools.count(1)
        self._ws: Any = None
        self._join_ref: str | None = None
        self._heartbeat_ref: str | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._joined = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = True
        self._attempts = 0
        self._last_traffic = time.monotonic()
        self._recent: OrderedDict[str, float] = OrderedDict()
        self._typing_refusals: dict[str, str] = {}
        self._chunk_refusals: dict[str, str] = {}
        self._bg: set[asyncio.Task] = set()
        self.sessions = 0
        self.fatal_error: SuiteError | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._supervise(), name="hermes-suite-client")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not an error
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for bg in list(self._bg):
            bg.cancel()
        self._end_session()

    @property
    def joined(self) -> bool:
        return self._joined.is_set()

    async def wait_joined(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._joined.wait(), timeout)

    def backoff_delay_ms(self, attempt: int) -> int:
        """Exponential from reconnect_min_ms, capped at reconnect_max_ms, then
        reduced by up to ``reconnect_jitter`` of itself (never above the cap)."""
        cfg = self._config
        base = min(cfg.reconnect_min_ms * (2 ** min(attempt, 30)), cfg.reconnect_max_ms)
        return max(1, int(base * (1.0 - cfg.reconnect_jitter * self._rng())))

    async def _supervise(self) -> None:
        while not self._stopped:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except InvalidSuiteURL as exc:
                self.fatal_error = exc
                self._log.error("%s; not reconnecting", exc)
                self._end_session()
                self._stopped = True
                self._call("fatal", exc)
                break
            except Exception as exc:  # noqa: BLE001 - every failure means "reconnect"
                self._log.warning("session ended: %s: %s", type(exc).__name__, exc)
            finally:
                self._end_session()
            if self._stopped:
                break
            self._call("disconnect")
            delay = self.backoff_delay_ms(self._attempts)
            self._attempts += 1
            self._log.info("reconnect scheduled in %dms (attempt %d)", delay, self._attempts + 1)
            await asyncio.sleep(delay / 1000)

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        cfg = self._config
        url = socket_url(cfg)
        self._log.info("connecting to %s (attempt %d)", url, self._attempts + 1)
        ws = await self._dial(connect, url)
        async with ws:
            self.sessions += 1
            self._ws = ws
            self._last_traffic = time.monotonic()
            self._log.info("socket open")
            reader = asyncio.create_task(self._read_loop(ws))
            tasks = {reader}
            try:
                await self._join(ws, reader)
                tasks.add(asyncio.create_task(self._heartbeat_loop(ws)))
                tasks.add(asyncio.create_task(self._watchdog_loop()))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _dial(self, connect: Any, url: str) -> Any:
        """Open the socket. The URL is parsed FIRST, with the same parser
        ``connect`` uses, so only a parse failure is classified as terminal;
        a network or handshake error during the dial still reconnects.

        A malformed URL is re-raised as ``InvalidSuiteURL`` carrying no part
        of the URL: ``websockets.exceptions.InvalidURI`` formats the WHOLE
        uri, query string and token included, into its message. The redacted
        error is raised OUTSIDE the ``except`` block so it has no
        ``__context__`` holding the original either."""
        from websockets.exceptions import InvalidURI
        from websockets.uri import parse_uri

        problem: str | None = None
        try:
            parse_uri(url)
        except InvalidURI:
            problem = "is not a valid ws:// or wss:// URI"
        except ValueError:
            # urllib's parse errors (bad port, bad IPv6 literal). Their text
            # does not carry the URL today; classified the same way anyway.
            problem = "could not be parsed"
        if problem is not None:
            raise InvalidSuiteURL(
                f"SUITE_URL {problem} (value withheld: the dial URL carries the token)"
            )
        return await connect(
            url,
            open_timeout=self._config.join_timeout_ms / 1000,
            ping_interval=None,
            max_size=MAX_INCOMING_FRAME,
            logger=self._ws_log,
        )

    def _end_session(self) -> None:
        self._joined.clear()
        self._ws = None
        self._heartbeat_ref = None
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(
                    ConnectionLost("the Suite socket closed before the push was acked")
                )

    async def _join(self, ws: Any, reader: asyncio.Task) -> None:
        ref = self._next_ref()
        self._join_ref = ref
        fut = asyncio.get_running_loop().create_future()
        self._pending[ref] = fut
        await self._send_frame(
            ws, [ref, ref, self._topic, "phx_join", {"client_info": client_info()}]
        )
        try:
            done, _ = await asyncio.wait(
                {fut, reader},
                timeout=self._config.join_timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._pending.pop(ref, None)
        if reader in done:
            reader.result()
        if fut not in done:
            self._log.warning("join %s timed out, scheduling reconnect", self._topic)
            raise _SessionEnded("join timed out")
        reply = fut.result()
        if reply.get("status") != "ok":
            self._log.warning(
                "failed to join %s: %s, scheduling reconnect",
                self._topic,
                json.dumps(reply.get("response")),
            )
            raise _SessionEnded("join refused")
        # Join success, not socket open, is the real "we're back".
        self._attempts = 0
        self._joined.set()
        self._log.info("joined %s", self._topic)
        self._call("joined")

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            self._last_traffic = time.monotonic()
            self._handle_frame(ws, raw)
        raise _SessionEnded(
            f"socket closed (code={ws.close_code} reason={ws.close_reason or 'n/a'})"
        )

    async def _heartbeat_loop(self, ws: Any) -> None:
        interval = self._config.heartbeat_ms / 1000
        while True:
            await asyncio.sleep(interval)
            if self._heartbeat_ref is not None:
                self._log.warning("heartbeat not acknowledged, forcing reconnect")
                raise _SessionEnded("heartbeat timeout")
            ref = self._next_ref()
            self._heartbeat_ref = ref
            await self._send_frame(ws, [None, ref, "phoenix", "heartbeat", {}])

    async def _watchdog_loop(self) -> None:
        cfg = self._config
        while True:
            await asyncio.sleep(cfg.watchdog_interval_ms / 1000)
            silent_ms = (time.monotonic() - self._last_traffic) * 1000
            if silent_ms > cfg.stale_traffic_ms:
                self._log.warning(
                    "watchdog: silent for %ds, forcing reconnect", round(silent_ms / 1000)
                )
                raise _SessionEnded("stale traffic")

    # ── inbound ──────────────────────────────────────────────────────────

    def _handle_frame(self, ws: Any, raw: Any) -> None:
        try:
            join_ref, ref, topic, event, payload = json.loads(raw)
        except (TypeError, ValueError):
            self._log.warning("dropping malformed frame")
            return

        if event == "phx_reply":
            if ref is not None and ref == self._heartbeat_ref:
                self._heartbeat_ref = None
                return
            fut = self._pending.get(ref) if ref is not None else None
            if fut is not None and not fut.done():
                fut.set_result(payload if isinstance(payload, dict) else {})
            return

        if topic != self._topic:
            return

        if event in ("phx_error", "phx_close"):
            if join_ref is None or join_ref == self._join_ref:
                self._log.warning("channel %s received %s, forcing reconnect", topic, event)
                self._spawn(ws.close())
            return

        payload = payload if isinstance(payload, dict) else {}
        if event == "attention":
            self._deliver_attention(payload)
        elif event == "ping":
            frame = [
                self._join_ref,
                self._next_ref(),
                self._topic,
                "pong",
                {"timestamp": _now_iso()},
            ]
            self._spawn(self._send_frame(ws, frame))
        elif event == "spaces_manifest":
            spaces = payload.get("spaces")
            self._call("spaces_manifest", spaces if isinstance(spaces, list) else [])
        elif event == "meeting_transcript":
            self._call("meeting_transcript", payload)
        else:
            self._log.debug("ignoring event %s", event)

    def _deliver_attention(self, payload: dict[str, Any]) -> None:
        now_ms = time.monotonic() * 1000
        window = self._config.dedupe_window_ms
        while self._recent:
            key, seen = next(iter(self._recent.items()))
            if now_ms - seen < window:
                break
            del self._recent[key]
        ident = attention_identity(payload)
        if ident in self._recent:
            self._log.debug("dropping duplicate attention inside the dedupe window")
            return
        self._recent[ident] = now_ms
        while len(self._recent) > DEDUPE_CAPACITY:
            self._recent.popitem(last=False)
        self._call("attention", payload)

    def _call(self, name: str, *args: Any) -> None:
        handler = self._handlers.get(name)
        if handler is None:
            return
        try:
            result = handler(*args)
            if inspect.isawaitable(result):
                self._spawn(result, label=f"{name} handler")
        except Exception as exc:  # noqa: BLE001 - a handler must never kill the reader
            self._log.error("%s handler raised %s: %s", name, type(exc).__name__, exc)

    def _spawn(self, coro: Any, label: str = "background send") -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)

        def done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                self._log.warning("%s failed: %s: %s", label, type(exc).__name__, exc)

        task.add_done_callback(done)

    # ── outbound ─────────────────────────────────────────────────────────

    def _next_ref(self) -> str:
        return str(next(self._refs))

    async def _send_frame(self, ws: Any, frame: list[Any]) -> None:
        await ws.send(json.dumps(frame))

    async def push(self, event: str, payload: dict[str, Any], space_id: str = "?") -> dict:
        """Push on the runtime channel and await the ack.

        Returns the ``response`` of an ok reply. Raises ``NotConnected``,
        ``PushRefused`` (carrying Suite's ``recoverable``), ``PushTimeout`` or
        ``ConnectionLost``.
        """
        ws = self._ws
        if ws is None or not self.joined:
            raise NotConnected(
                f"not connected to Suite: the runtime channel is not joined, "
                f"so the {event} to space {space_id} was not delivered"
            )
        ref = self._next_ref()
        fut = asyncio.get_running_loop().create_future()
        self._pending[ref] = fut
        timeout_ms = self._config.push_timeout_ms
        try:
            await self._send_frame(ws, [self._join_ref, ref, self._topic, event, payload])
            reply = await asyncio.wait_for(fut, timeout_ms / 1000)
        except TimeoutError:
            self._log.warning("%s to space %s: no ack after %dms", event, space_id, timeout_ms)
            raise PushTimeout(
                f"timed out after {timeout_ms}ms waiting for Suite to ack the {event} "
                f"to space {space_id}"
            ) from None
        except SuiteError:
            raise
        except Exception as exc:  # the socket died under the send
            raise ConnectionLost(
                f"the Suite socket failed while sending the {event} to space {space_id} "
                f"({type(exc).__name__})"
            ) from None
        finally:
            self._pending.pop(ref, None)

        response = reply.get("response")
        response = response if isinstance(response, dict) else {}
        if reply.get("status") == "ok":
            return response
        error = response.get("error")
        raise PushRefused(
            error
            if isinstance(error, str)
            else f"{event} to space {space_id} was refused by Suite",
            recoverable=response.get("recoverable") is True,
            response=response,
        )

    async def send_reply(self, space_id: str, content: str) -> dict:
        return await self.push("reply", {"space_id": space_id, "content": content}, space_id)

    async def send_reply_with_media(
        self, space_id: str, content: str, attachments: list[dict[str, Any]]
    ) -> dict:
        """``attachments``: ``{filename, content_type, data}``; ``data`` is bytes
        (encoded here) or an already-base64 str."""
        encoded = []
        for att in attachments:
            data = att["data"]
            if isinstance(data, bytes | bytearray):
                data = base64.b64encode(data).decode("ascii")
            if len(data) > MAX_ATTACHMENT_BYTES * 4 // 3:
                raise AttachmentTooLarge(
                    f"attachment {att['filename']!r} exceeds Suite's 10 MiB per-file cap"
                )
            encoded.append(
                {"filename": att["filename"], "content_type": att["content_type"], "data": data}
            )
        if sum(len(a["data"]) for a in encoded) > MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3:
            raise AttachmentTooLarge("attachments exceed Suite's 25 MiB total cap")
        payload = {"space_id": space_id, "content": content, "attachments": encoded}
        return await self.push("reply_with_media", payload, space_id)

    def send_typing(self, space_id: str, typing: bool) -> None:
        """Fire-and-forget; a refusal is recorded per space (``last_typing_refusal``)."""
        self._fire("typing", {"space_id": space_id, "typing": typing}, self._typing_refusals)

    def send_reply_chunk(self, space_id: str, chunk_id: str, text: str, done: bool) -> None:
        """Fire-and-forget; a refusal is recorded per space (``last_chunk_refusal``)."""
        payload = {"space_id": space_id, "chunk_id": chunk_id, "text": text, "done": done}
        self._fire("reply_chunk", payload, self._chunk_refusals)

    def last_typing_refusal(self, space_id: str) -> str | None:
        return self._typing_refusals.get(space_id)

    def last_chunk_refusal(self, space_id: str) -> str | None:
        return self._chunk_refusals.get(space_id)

    def _fire(self, event: str, payload: dict[str, Any], refusals: dict[str, str]) -> None:
        space_id = payload["space_id"]

        async def run() -> None:
            try:
                await self.push(event, payload, space_id)
                refusals.pop(space_id, None)
            except PushRefused as exc:
                refusals[space_id] = str(exc)
                self._log.info("%s for space %s refused: %s", event, space_id, exc)
            except SuiteError as exc:
                self._log.debug("%s for space %s not acked: %s", event, space_id, exc)

        self._spawn(run(), label=event)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
