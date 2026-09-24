from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
from pathlib import Path

import pytest

from museflow.api.app import RequestBodyLimitMiddleware
from museflow.reference_assets.streaming import UploadTooLargeError, stage_upload


def _http_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/assets",
        "raw_path": b"/api/v1/assets",
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }


@pytest.mark.parametrize("length_header", [None, (b"content-length", b"1")])
def test_raw_multipart_limit_uses_actual_asgi_bytes(
    length_header: tuple[bytes, bytes] | None,
) -> None:
    app_called = False
    sent: list[dict[str, object]] = []
    messages = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"defg", "more_body": False},
        ]
    )

    async def app(scope, receive, send) -> None:
        nonlocal app_called
        app_called = True

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    headers = [length_header] if length_header is not None else []
    asyncio.run(RequestBodyLimitMiddleware(app, max_bytes=6)(_http_scope(headers), receive, send))

    assert not app_called
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert b"MULTIPART_BODY_TOO_LARGE" in sent[1]["body"]


def test_raw_multipart_limit_replays_bounded_body_to_parser() -> None:
    app_received: list[bytes] = []
    sent: list[dict[str, object]] = []
    original = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]

    async def app(scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            app_received.append(message["body"])
            if not message["more_body"]:
                break

    async def receive():
        return original.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(RequestBodyLimitMiddleware(app, max_bytes=6)(_http_scope([]), receive, send))

    assert b"".join(app_received) == b"abcdef"
    assert sent == []


def test_streaming_upload_hashes_chunks_and_cleans_temp_file_on_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []
    original_mkstemp = tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        created_paths.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(tempfile, "mkstemp", capture_mkstemp)

    data = b"seven!!"
    source = io.BytesIO(data)

    async def read_source(size: int) -> bytes:
        return source.read(size)

    accepted = asyncio.run(stage_upload(read_source, max_bytes=len(data)))
    assert accepted.path.read_bytes() == data
    assert accepted.sha256 == hashlib.sha256(data).hexdigest()
    accepted.path.unlink()

    rejected_source = io.BytesIO(data + b"!")

    async def read_rejected(size: int) -> bytes:
        return rejected_source.read(size)

    with pytest.raises(UploadTooLargeError):
        asyncio.run(stage_upload(read_rejected, max_bytes=len(data)))
    assert not created_paths[-1].exists()
