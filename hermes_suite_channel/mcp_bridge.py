"""stdio MCP server that relays to Suite's HTTP MCP endpoint, reading the
bearer token from a file.

Why it exists: Hermes resolves an HTTP MCP server's ``headers`` only through
``${VAR}`` interpolation from the environment (tools/mcp_tool_config.py
``_interpolate_env_vars``), and its own ``hermes mcp add --auth header`` stores
the token in $HERMES_HOME/.env for the same interpolation. Either way the
token sits in the gateway's ``os.environ``, which Hermes re-fills from .env on
every turn and every MCP config load, and which children spawned without a
scrub inherit (tools/env_probe.py). A stdio server is the one MCP shape whose
secret can live somewhere else: Hermes spawns it with a filtered environment
(``_build_safe_env``), and this process reads the token from
``--token-file`` itself, so the token is in no process environment at all.

Wire protocol: one JSON-RPC message per line on stdin, one per line on stdout
(MCP stdio). Each message is POSTed to ``--url`` (MCP Streamable HTTP): a JSON
body is relayed as is, a ``text/event-stream`` body is relayed event by event,
202/204 relays nothing. ``Mcp-Session-Id`` and ``MCP-Protocol-Version`` are
carried forward as the transport requires. Suite's endpoint answers JSON,
keeps no session and refuses the optional GET stream (405,
platform_web/controllers/mcp_controller.ex), so server-initiated messages are
not relayed.

Standard library only: it runs on whatever interpreter install.sh found for
Hermes, never imports the plugin package, and needs nothing installed.

The token is read from the file for every request (a rotated token is picked
up without a restart), sent only in the Authorization header, and never
written to stdout, stderr or an error message.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

SERVER_ERROR = -32000
PARSE_ERROR = -32700
MAX_WORKERS = 8


class BridgeError(Exception):
    """A relay failure, phrased without the token."""


def read_token(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            mode = os.fstat(f.fileno()).st_mode
            token = f.readline().strip()
    except OSError as exc:
        raise BridgeError(f"cannot read token file {path}: {type(exc).__name__}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BridgeError(f"token file {path} is readable by group or other; chmod 600 it")
    if not token:
        raise BridgeError(f"token file {path} is empty")
    return token


def parse_sse(body: str) -> list[Any]:
    """The JSON messages carried by the ``data:`` fields of an SSE body."""
    messages, data = [], []
    for line in body.splitlines() + [""]:
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                try:
                    messages.append(json.loads(payload))
                except ValueError:
                    continue
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    return messages


class Bridge:
    def __init__(self, url: str, token_file: str, timeout: float) -> None:
        self.url = url
        self.token_file = token_file
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._out = threading.Lock()
        self._state = threading.Lock()

    # ── output ─────────────────────────────────────────────────────────

    def write(self, message: Any) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._out:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def error_reply(self, msg_id: Any, text: str, code: int = SERVER_ERROR) -> None:
        self.write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}})

    # ── relay ──────────────────────────────────────────────────────────

    def post(self, message: Any) -> list[Any]:
        token = read_token(self.token_file)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        with self._state:
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            if self.protocol_version:
                headers["MCP-Protocol-Version"] = self.protocol_version
        body = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                ctype = response.headers.get("Content-Type", "")
                session = response.headers.get("Mcp-Session-Id")
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"Suite MCP answered HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            reason = type(getattr(exc, "reason", exc)).__name__
            raise BridgeError(f"Suite MCP unreachable ({reason})") from None
        except (TimeoutError, OSError) as exc:
            raise BridgeError(f"Suite MCP request failed ({type(exc).__name__})") from None
        if session:
            with self._state:
                self.session_id = session
        if status in (202, 204) or not raw.strip():
            return []
        if "text/event-stream" in ctype:
            replies = parse_sse(raw)
        else:
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise BridgeError("Suite MCP answered a body that is not JSON") from None
            replies = parsed if isinstance(parsed, list) else [parsed]
        self._note_protocol(message, replies)
        return replies

    def _note_protocol(self, message: Any, replies: list[Any]) -> None:
        if not (isinstance(message, dict) and message.get("method") == "initialize"):
            return
        for reply in replies:
            result = reply.get("result") if isinstance(reply, dict) else None
            if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
                with self._state:
                    self.protocol_version = result["protocolVersion"]

    def handle(self, message: Any) -> None:
        msg_id = message.get("id") if isinstance(message, dict) else None
        is_request = isinstance(message, dict) and "method" in message and "id" in message
        try:
            for reply in self.post(message):
                self.write(reply)
        except BridgeError as exc:
            if is_request:
                self.error_reply(msg_id, str(exc))
            else:
                print(f"suite-mcp-bridge: {exc}", file=sys.stderr, flush=True)

    def run(self, stdin) -> None:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            initialized = False
            for raw in stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.error_reply(None, "parse error", PARSE_ERROR)
                    continue
                is_init = isinstance(message, dict) and message.get("method") == "initialize"
                is_request = isinstance(message, dict) and "id" in message
                if is_init or not initialized or not is_request:
                    # In order, on this thread: initialize (the session id
                    # and protocol version it returns must be in place for
                    # what follows), and every notification and response
                    # (notifications/initialized must reach the server before
                    # any request sent after it). Only requests run in
                    # parallel; their replies carry ids, so order is free.
                    self.handle(message)
                    initialized = initialized or is_init
                else:
                    pool.submit(self.handle, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--url", required=True, help="Suite MCP endpoint, e.g. https://host/mcp")
    parser.add_argument("--token-file", required=True, help="file holding the runtime token")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-request timeout, s")
    args = parser.parse_args(argv)
    try:
        read_token(args.token_file)  # fail at start, not on the first tool call
    except BridgeError as exc:
        print(f"suite-mcp-bridge: {exc}", file=sys.stderr)
        return 2
    Bridge(args.url, args.token_file, args.timeout).run(sys.stdin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
