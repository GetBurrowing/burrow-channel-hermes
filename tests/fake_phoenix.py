"""A hermetic fake of core's runtime socket: Phoenix V2 JSON frames over
``websockets.serve`` on 127.0.0.1, port chosen by the OS. No network."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.server import serve

# The fake's own websockets logger would log the request target (and so the
# token) at DEBUG. It is not the code under test, so it is silenced and kept
# out of caplog: a leak the log-capture test reports must be the client's.
# `propagate = False` alone is NOT enough: measured under pytest 9.1.1, caplog
# still captured this logger's "< GET ...?token=..." records. `disabled` drops
# them before any handler runs.
_SILENT = logging.getLogger("fake_phoenix")
_SILENT.propagate = False
_SILENT.disabled = True

OK: tuple[str, dict] = ("ok", {})


class FakePhoenix:
    def __init__(self) -> None:
        # Next N handshakes are refused with HTTP 403.
        self.reject_handshakes = 0
        # Next N joins are answered with an error reply.
        self.refuse_joins = 0
        # event -> ("ok"|"error", response) or None (never reply).
        self.replies: dict[str, tuple[str, dict] | None] = {"pong": None}
        self.frames: list[tuple[int, list[Any]]] = []
        self.request_paths: list[str] = []
        self.connections: list[Any] = []
        self.topic: str | None = None
        self._changed = asyncio.Condition()
        self._server: Any = None

    async def __aenter__(self) -> FakePhoenix:
        self._server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request, logger=_SILENT
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/runtime/ws"

    def _process_request(self, connection: Any, request: Any) -> Any:
        self.request_paths.append(request.path)
        if self.reject_handshakes > 0:
            self.reject_handshakes -= 1
            return connection.respond(403, "forbidden\n")
        return None

    async def _handle(self, conn: Any) -> None:
        index = len(self.connections)
        self.connections.append(conn)
        try:
            async for raw in conn:
                frame = json.loads(raw)
                async with self._changed:
                    self.frames.append((index, frame))
                    self._changed.notify_all()
                join_ref, ref, topic, event, _payload = frame
                if event == "phx_join":
                    self.topic = topic
                    if self.refuse_joins > 0:
                        self.refuse_joins -= 1
                        await self._reply(conn, frame, "error", {"reason": "unauthorized"})
                    else:
                        await self._reply(conn, frame, "ok", {})
                    continue
                behaviour = self.replies.get(event, OK)
                if behaviour is not None:
                    await self._reply(conn, frame, *behaviour)
        except Exception:  # noqa: BLE001 - a client hanging up is normal here
            pass

    async def _reply(self, conn: Any, frame: list[Any], status: str, response: dict) -> None:
        join_ref, ref, topic, _event, _payload = frame
        await conn.send(
            json.dumps(
                [join_ref, ref, topic, "phx_reply", {"status": status, "response": response}]
            )
        )

    async def push(self, event: str, payload: dict[str, Any]) -> None:
        await self.connections[-1].send(json.dumps([None, None, self.topic, event, payload]))

    async def close_current(self) -> None:
        await self.connections[-1].close()

    def events(self, event: str) -> list[tuple[int, list[Any]]]:
        return [(i, f) for i, f in self.frames if f[3] == event]

    async def wait_for(self, predicate, timeout: float = 3.0) -> None:
        async def waiter() -> None:
            async with self._changed:
                await self._changed.wait_for(predicate)

        await asyncio.wait_for(waiter(), timeout)
