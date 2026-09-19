from __future__ import annotations

import argparse
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from museflow.assets import MinioResultAssetStore, ResultAssetStore, validate_result
from museflow.providers import (
    DASHSCOPE_SIZE,
    DashScopeProvider,
    GenerationRequest,
    GenerationResult,
    ProviderError,
)


class SmokeVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokeAssetSummary:
    signed_status: int
    anonymous_status: int
    expired_status: int


def verify_result_asset(
    result: GenerationResult,
    *,
    store: ResultAssetStore,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
) -> SmokeAssetSummary:
    if result.content_type != "image/png":
        raise SmokeVerificationError("RESULT_NOT_PNG")
    try:
        _, size, digest = validate_result(result.content, result.content_type)
    except ValueError as error:
        raise SmokeVerificationError("RESULT_IMAGE_INVALID") from error
    if digest != result.result_digest or digest != result.metadata.get("sha256"):
        raise SmokeVerificationError("RESULT_CHECKSUM_MISMATCH")
    if str(size) != result.metadata.get("size_bytes"):
        raise SmokeVerificationError("RESULT_SIZE_MISMATCH")
    if (result.metadata.get("width"), result.metadata.get("height")) != ("1280", "1280"):
        raise SmokeVerificationError("RESULT_DIMENSIONS_INVALID")

    stored = store.put_result(
        task_id=uuid4(),
        attempt_id=uuid4(),
        content=result.content,
        content_type=result.content_type,
    )
    if (stored.content_type, stored.size_bytes, stored.sha256) != (
        result.content_type,
        size,
        digest,
    ):
        raise SmokeVerificationError("STORED_RESULT_MISMATCH")

    signed_url = store.presigned_download(stored.object_key, expires_seconds=5)
    unsigned_url = urlsplit(signed_url)._replace(query="", fragment="").geturl()
    try:
        signed = client.get(signed_url)
        if signed.status_code != 200 or hashlib.sha256(signed.content).hexdigest() != digest:
            raise SmokeVerificationError("SIGNED_DOWNLOAD_INVALID")
        anonymous = client.get(unsigned_url)
        if anonymous.status_code != 403:
            raise SmokeVerificationError("ANONYMOUS_DOWNLOAD_NOT_PRIVATE")
        sleep(6)
        expired = client.get(signed_url)
        if expired.status_code != 403:
            raise SmokeVerificationError("EXPIRED_SIGNATURE_ACCEPTED")
    except httpx.HTTPError as error:
        raise SmokeVerificationError("MINIO_HTTP_ERROR") from error
    return SmokeAssetSummary(signed.status_code, anonymous.status_code, expired.status_code)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicitly authorized DashScope smoke request."
    )
    parser.add_argument(
        "--authorize-real-request",
        action="store_true",
        help="confirm one real request, up to about CNY 0.20, with no automatic retry",
    )
    args = parser.parse_args()

    configured = {
        name: bool(os.environ.get(name)) for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_API_HOST")
    }
    print(
        "DASHSCOPE_API_KEY configured:",
        "yes" if configured["DASHSCOPE_API_KEY"] else "no",
    )
    print(
        "DASHSCOPE_API_HOST configured:",
        "yes" if configured["DASHSCOPE_API_HOST"] else "no",
    )
    if not all(configured.values()):
        print("No request sent: both required environment variables must exist.")
        return 2
    if not args.authorize_real_request:
        print(
            "No request sent. Re-run with --authorize-real-request only after confirming "
            "Beijing region/workspace, model permission, billing, one request up to about "
            "CNY 0.20, no automatic retry, and immediate stop on any explicit error."
        )
        return 2

    try:
        store = MinioResultAssetStore()
        store.check_ready()
        provider = DashScopeProvider()
    except Exception:
        print("Preflight failed: local store or provider configuration is not ready.")
        return 2
    try:
        result = provider.generate(
            GenerationRequest(
                prompt="A small ceramic lighthouse on a quiet blue sea at sunrise.",
                size_preset=DASHSCOPE_SIZE,
            ),
            request_key="controlled-smoke",
            remote_request_id=None,
        )
        print(
            "Result host diagnostic:",
            f"allowlisted={result.metadata.get('result_host_allowlisted', 'unknown')},",
            f"host_digest={result.metadata.get('result_host_digest', 'unknown')}",
        )
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            asset_summary = verify_result_asset(result, store=store, client=client)
    except ProviderError as error:
        print("Provider error:", error.code)
        if error.diagnostic:
            print("Result host diagnostic:", error.diagnostic)
        return 1
    except SmokeVerificationError as error:
        print("Asset verification failed:", str(error))
        return 1
    except Exception:
        print("Smoke verification failed: unexpected local error.")
        return 1
    finally:
        provider.close()

    print("Provider: dashscope")
    print("HTTP statuses:", result.metadata.get("poll_http_statuses", "unknown"))
    print("Task status sequence:", result.metadata.get("task_status_sequence", "unknown"))
    print(
        "Result:",
        result.metadata.get("content_type", "unknown"),
        result.metadata.get("width", "unknown"),
        "x",
        result.metadata.get("height", "unknown"),
        result.metadata.get("size_bytes", "unknown"),
        "bytes",
        result.metadata.get("sha256", "unknown"),
    )
    print("MinIO: stored in private bucket")
    print("Signed download HTTP status:", asset_summary.signed_status)
    print("Anonymous download HTTP status:", asset_summary.anonymous_status)
    print("Expired signature HTTP status:", asset_summary.expired_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
