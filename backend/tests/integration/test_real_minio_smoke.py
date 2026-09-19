from __future__ import annotations

import hashlib
import os
import struct
import zlib

import httpx
import pytest

from museflow.assets import MinioResultAssetStore, StoredAsset
from museflow.dashscope_smoke import verify_result_asset
from museflow.providers import GenerationResult


def _png_chunk(name: bytes, data: bytes) -> bytes:
    body = name + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _valid_png() -> bytes:
    row = b"\x00" + b"\x00\x00\x00" * 1280
    pixels = zlib.compress(row * 1280)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1280, 1280, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def test_real_minio_result_is_private_signed_and_expires() -> None:
    if os.environ.get("MUSEFLOW_RUN_REAL_MINIO_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REAL_MINIO_TEST=1 with Compose MinIO")

    store = MinioResultAssetStore()
    store.check_ready()
    content = _valid_png()
    digest = hashlib.sha256(content).hexdigest()
    result = GenerationResult(
        provider_name="dashscope",
        provider_request_id="fixture-only",
        result_digest=digest,
        metadata={
            "sha256": digest,
            "width": "1280",
            "height": "1280",
            "size_bytes": str(len(content)),
        },
        content=content,
        content_type="image/png",
    )
    saved: list[StoredAsset] = []

    class CapturingStore:
        def put_result(self, **kwargs: object) -> StoredAsset:
            asset = store.put_result(**kwargs)  # type: ignore[arg-type]
            saved.append(asset)
            return asset

        def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
            return store.presigned_download(object_key, expires_seconds=expires_seconds)

        def check_ready(self) -> None:
            store.check_ready()

    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            summary = verify_result_asset(result, store=CapturingStore(), client=client)
        assert (summary.signed_status, summary.anonymous_status, summary.expired_status) == (
            200,
            403,
            403,
        )
    finally:
        for asset in saved:
            store._client.remove_object(store._bucket, asset.object_key)
