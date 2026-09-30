import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_suite_channel import inbound_media as media

AID = "01a0eed9-97be-7001-bb30-a72f5ae518c8"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII="
)
ATT = {"url": "/chat/attachments/" + AID, "content_type": "image/png"}


def inline(raw=PNG):
    return {"byte_size": len(raw), "data_base64": base64.b64encode(raw).decode()}


def test_download_private_file_no_filename_trust(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "fetch_inline", lambda *args: inline())
    path, mime, size = media.download_image(
        {**ATT, "filename": "../../evil.exe"}, "wss://suite.test/socket", "secret", tmp_path
    )
    assert Path(path).parent == tmp_path
    assert Path(path).read_bytes() == PNG
    assert Path(path).stat().st_mode & 0o777 == 0o600
    assert Path(path).suffix == ".png"
    assert mime == "image/png" and size == len(PNG)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/chat/attachments/" + AID,
        "//evil.test/x",
        "/chat/attachments/../../etc/passwd",
        "/chat/attachments/" + AID + "?x=1",
    ],
)
def test_refuses_arbitrary_urls(url):
    with pytest.raises(media.MediaError):
        media.attachment_id({"url": url})


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"byte_size": True, "data_base64": "a"},
        {"byte_size": 10, "data_base64": "!!!"},
        {**inline(), "byte_size": 1},
        {**inline(), "byte_size": media.MAX_IMAGE_BYTES + 1},
        inline(b"<svg></svg>"),
    ],
)
def test_invalid_payload(data):
    with pytest.raises(media.MediaError):
        media.decode_image(data)


def test_budget_and_metadata_refused_before_fetch(monkeypatch, tmp_path):
    fetch = MagicMock(return_value=inline())
    monkeypatch.setattr(media, "fetch_inline", fetch)
    with pytest.raises(media.MediaError):
        media.download_image(
            {**ATT, "byte_size": media.MAX_IMAGE_BYTES + 1}, "wss://suite.test", "secret", tmp_path
        )
    fetch.assert_not_called()
    with pytest.raises(media.MediaError):
        media.download_image(ATT, "wss://suite.test", "secret", tmp_path, remaining=1)
    assert not list(tmp_path.iterdir())


def test_trigger_only_and_malformed():
    assert media.image_attachments({"history": [{"attachments": [ATT]}]}) == []
    assert media.image_attachments({"message": {"attachments": "bad"}}) == []
    assert media.image_attachments({"message": {"attachments": [None, ATT, {}]}}) == [ATT]


def test_mcp_transport_bounded_and_authenticated(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(
        {"result": {"content": [{"type": "text", "text": json.dumps(inline())}]}}
    ).encode()
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(media.urllib.request, "build_opener", lambda *args: opener)
    assert media.fetch_inline("wss://suite.test/runtime/socket", "secret", AID) == inline()
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://suite.test/mcp"
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data)["params"]["arguments"] == {"attachment_id": AID}
    response.read.assert_called_once_with(media.MAX_RESPONSE_BYTES + 1)
    response.read.return_value = b"x" * (media.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(media.MediaError, match="exceeds"):
        media.fetch_inline("wss://suite.test", "secret", AID)


def test_redirect_refused_and_error_redacted(monkeypatch):
    with pytest.raises(media.MediaError):
        media._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")
    opener = MagicMock()
    opener.open.side_effect = OSError("secret")
    monkeypatch.setattr(media.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(media.MediaError) as exc:
        media.fetch_inline("wss://suite.test", "secret", AID)
    assert "secret" not in str(exc.value)
