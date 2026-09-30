# Bounded authenticated image downloads; no model-visible base64.
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 64 * 1024
MAX_IMAGES = 8
MAX_TOTAL_BYTES = 25 * 1024 * 1024
_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PATH = re.compile(r"/chat/attachments/(" + _ID + r")")


class MediaError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MediaError("attachment endpoint redirected")


def image_attachments(payload):
    # Triggering message only, never history or arbitrary remote URLs.
    message = payload.get("message")
    attachments = message.get("attachments", []) if isinstance(message, dict) else []
    if not isinstance(attachments, list):
        return []
    return [
        a
        for a in attachments
        if isinstance(a, dict)
        and isinstance(a.get("content_type"), str)
        and a["content_type"].lower().startswith("image/")
    ]


def attachment_id(attachment):
    url = attachment.get("url")
    match = _PATH.fullmatch(url) if isinstance(url, str) else None
    if match:
        return match[1]
    value = attachment.get("id")
    if url is None and isinstance(value, str) and re.fullmatch(_ID, value):
        return value
    raise MediaError("invalid attachment reference")


def mcp_url(suite_url):
    parts = urlsplit(suite_url)
    if parts.scheme not in ("ws", "wss", "http", "https") or not parts.hostname:
        raise MediaError("invalid Suite endpoint")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise MediaError("invalid Suite endpoint")
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    return urlunsplit((scheme, parts.netloc, "/mcp", "", ""))


def fetch_inline(suite_url, token, aid):
    request = urllib.request.Request(
        mcp_url(suite_url),
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "attachment.get_inline", "arguments": {"attachment_id": aid}},
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MediaError("attachment response exceeds limit")
        reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get("error"):
            raise MediaError("attachment retrieval refused")
        result = reply["result"]
        if result.get("isError"):
            raise MediaError("attachment retrieval refused")
        return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
    except MediaError:
        raise
    except Exception:
        raise MediaError("attachment retrieval failed") from None


def decode_image(data):
    try:
        size = data["byte_size"]
        encoded = data["data_base64"]
        if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES:
            raise ValueError
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != size:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise MediaError("invalid attachment data or size") from None
    if raw.startswith(bytes.fromhex("89504e470d0a1a0a")):
        return raw, "image/png", ".png"
    if raw.startswith(bytes.fromhex("ffd8ff")):
        return raw, "image/jpeg", ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return raw, "image/gif", ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return raw, "image/webp", ".webp"
    raise MediaError("unsupported image signature")


def download_image(attachment, suite_url, token, cache_dir, remaining=MAX_TOTAL_BYTES):
    aid = attachment_id(attachment)
    declared = attachment.get("byte_size")
    if declared is not None and (
        type(declared) is not int or not 0 < declared <= min(MAX_IMAGE_BYTES, remaining)
    ):
        raise MediaError("attachment exceeds size limit")
    raw, mime, suffix = decode_image(fetch_inline(suite_url, token, aid))
    if len(raw) > remaining:
        raise MediaError("attachments exceed total size limit")
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(prefix="suite-", suffix=suffix, dir=cache)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path, mime, len(raw)
