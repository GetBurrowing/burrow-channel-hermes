"""hermes_suite_channel/mcp_bridge.py as Hermes runs it: a subprocess speaking
MCP stdio, relaying to an HTTP endpoint shaped like Burrow's
(platform_web/controllers/mcp_controller.ex: JSON replies, 204 for a
notification, no session)."""

import json
import os
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_suite_channel import __version__
from hermes_suite_channel.mcp_bridge import USER_AGENT, parse_sse

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "hermes_suite_channel" / "mcp_bridge.py"
TOKEN = "tok-bridge-SENTINEL-01a0d6c5"


class FakeSuiteMCP:
    def __init__(self, token: str, sse: bool = False):
        self.token = token
        self.sse = sse
        self.seen: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append(
                    {"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                )
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                if "id" not in body:
                    self.send_response(204)
                    self.end_headers()
                    return
                if body["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
                elif body["method"] == "tools/list":
                    result = {"tools": [{"name": "memory_search"}]}
                else:
                    result = {"echo": body.get("params")}
                reply = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
                self.send_response(200)
                if fake.sse:
                    self.send_header("Content-Type", "text/event-stream")
                    payload = f"event: message\ndata: {reply}\n\n".encode()
                else:
                    self.send_header("Content-Type", "application/json")
                    payload = reply.encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def token_file(tmp_path):
    tf = tmp_path / "runtime-token"
    tf.write_text(TOKEN + "\n")
    tf.chmod(0o600)
    return tf


def run_bridge(url, token_file, messages, env=None):
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), "--url", url, "--token-file", str(token_file)],
        input=lines,
        capture_output=True,
        text=True,
        timeout=30,
        env=env if env is not None else {"PATH": os.environ["PATH"]},
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc, replies


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


@pytest.mark.parametrize("sse", [False, True])
def test_relays_requests_and_carries_the_token_only_in_the_header(token_file, sse):
    fake = FakeSuiteMCP(TOKEN, sse=sse)
    try:
        proc, replies = run_bridge(fake.url, token_file, [INIT, INITIALIZED, LIST])
    finally:
        fake.close()
    assert proc.returncode == 0, proc.stderr
    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id[2]["result"]["tools"] == [{"name": "memory_search"}]
    assert len(replies) == 2  # the notification's 204 relays nothing
    assert [s["body"].get("method") for s in fake.seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert all(s["headers"]["authorization"] == f"Bearer {TOKEN}" for s in fake.seen)
    # The protocol version initialize returned rides on every later request.
    assert fake.seen[2]["headers"].get("mcp-protocol-version") == "2025-06-18"
    assert TOKEN not in proc.stdout + proc.stderr


def test_every_request_carries_the_burrow_user_agent(token_file):
    """Cloudflare in front of some Burrow hosts answers urllib's default
    Python-urllib/3.x with 403 error code 1010, so the bridge names itself."""
    fake = FakeSuiteMCP(TOKEN)
    try:
        proc, replies = run_bridge(fake.url, token_file, [INIT, INITIALIZED, LIST])
    finally:
        fake.close()
    assert proc.returncode == 0, proc.stderr
    assert len(fake.seen) == 3
    assert [s["headers"].get("user-agent") for s in fake.seen] == [
        "burrow-channel-hermes/0.4.1"
    ] * 3


def test_version_strings_match_pyproject():
    """The bridge runs by path and never imports the package, so its
    User-Agent spells the version; this keeps every copy on pyproject's."""
    with open(ROOT / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]
    plugin_version = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "plugin.yaml").read_text().splitlines()
        if line.startswith("version:")
    )
    assert __version__ == version
    assert plugin_version == version
    assert USER_AGENT == f"burrow-channel-hermes/{version}"


def test_a_refused_token_is_an_error_reply_that_does_not_carry_it(token_file):
    fake = FakeSuiteMCP("some-other-token")
    try:
        proc, replies = run_bridge(fake.url, token_file, [INIT])
    finally:
        fake.close()
    assert proc.returncode == 0
    (reply,) = replies
    assert reply["id"] == 1 and "HTTP 401" in reply["error"]["message"]
    assert TOKEN not in proc.stdout + proc.stderr


def test_unreachable_endpoint_is_an_error_reply(token_file):
    proc, replies = run_bridge("http://127.0.0.1:9/mcp", token_file, [INIT])
    assert replies[0]["id"] == 1 and "unreachable" in replies[0]["error"]["message"]
    assert TOKEN not in proc.stdout + proc.stderr


def test_group_readable_token_file_stops_the_bridge_at_start(token_file):
    token_file.chmod(0o644)
    proc, replies = run_bridge("http://127.0.0.1:9/mcp", token_file, [INIT])
    assert proc.returncode == 2 and replies == []
    assert "chmod 600" in proc.stderr and TOKEN not in proc.stderr


def test_the_bridge_environment_never_holds_the_token(token_file, tmp_path):
    """Hermes spawns a stdio server with a filtered env (_build_safe_env);
    the bridge reads the file, so its /proc/<pid>/environ has no token. The
    positive control is a canary the test DID put in the env."""
    fake = FakeSuiteMCP(TOKEN)
    env = {"PATH": os.environ["PATH"], "CANARY_01A0D6C5": "canary-value-01a0d6c5"}
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--url", fake.url, "--token-file", str(token_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        proc.stdin.write(json.dumps(INIT) + "\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == 1  # the token is in use now
        environ = Path(f"/proc/{proc.pid}/environ").read_bytes()
        assert b"canary-value-01a0d6c5" in environ  # the check can fire
        assert TOKEN.encode() not in environ
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        assert TOKEN.encode() not in cmdline
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        fake.close()


def test_parse_sse_reads_data_fields():
    body = 'event: message\ndata: {"a": 1}\n\n: comment\ndata:{"b":\ndata: 2}\n\n'
    assert parse_sse(body) == [{"a": 1}, {"b": 2}]
