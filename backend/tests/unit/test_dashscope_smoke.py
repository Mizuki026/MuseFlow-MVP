from __future__ import annotations

import hashlib

import httpx
import pytest

from museflow.assets import StoredAsset
from museflow.dashscope_smoke import SmokeVerificationError, verify_result_asset
from museflow.providers import GenerationResult

PNG = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (1280).to_bytes(4, "big") * 2
    + b"\x08\x02\x00\x00\x00"
)


def _result() -> GenerationResult:
    digest = hashlib.sha256(PNG).hexdigest()
    return GenerationResult(
        provider_name="dashscope",
        provider_request_id="redacted-task",
        result_digest=digest,
        metadata={"sha256": digest, "width": "1280", "height": "1280", "size_bytes": str(len(PNG))},
        content=PNG,
        content_type="image/png",
    )


class _Store:
    def __init__(self) -> None:
        self.saved = False

    def put_result(self, *, task_id, attempt_id, content: bytes, content_type: str) -> StoredAsset:
        assert content == PNG
        assert content_type == "image/png"
        self.saved = True
        return StoredAsset(
            "results/isolated/result.png",
            content_type,
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
        assert object_key == "results/isolated/result.png"
        assert expires_seconds == 5
        return (
            "http://127.0.0.1:9000/museflow-results/results/isolated/result.png?signature=fixture"
        )

    def check_ready(self) -> None:
        return None


def test_one_downloaded_result_is_stored_and_checked_with_private_signed_url() -> None:
    store = _Store()
    statuses: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = 200 if request.url.query and not sleeps else 403
        statuses.append(status)
        return httpx.Response(status, content=PNG if status == 200 else b"")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = verify_result_asset(_result(), store=store, client=client, sleep=sleeps.append)

    assert store.saved
    assert statuses == [200, 403, 403]
    assert sleeps == [6]
    assert (summary.signed_status, summary.anonymous_status, summary.expired_status) == (
        200,
        403,
        403,
    )


def test_invalid_result_is_rejected_before_storage() -> None:
    store = _Store()
    result = _result()
    result.metadata["sha256"] = "0" * 64

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with pytest.raises(SmokeVerificationError, match="RESULT_CHECKSUM_MISMATCH"):
            verify_result_asset(result, store=store, client=client, sleep=lambda _: None)

    assert not store.saved
