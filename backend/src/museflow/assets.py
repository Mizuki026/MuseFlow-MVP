from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from minio import Minio

MAX_RESULT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredAsset:
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str


class ResultAssetStore(Protocol):
    def put_result(
        self,
        *,
        task_id: UUID,
        attempt_id: UUID,
        content: bytes,
        content_type: str,
    ) -> StoredAsset: ...

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str: ...

    def check_ready(self) -> None: ...


def validate_result(content: bytes, content_type: str) -> tuple[str, int, str]:
    allowed = {
        "image/png": (b"\x89PNG\r\n\x1a\n", "png"),
        "image/jpeg": (b"\xff\xd8\xff", "jpg"),
        "image/webp": (b"RIFF", "webp"),
    }
    if content_type not in allowed:
        raise ValueError("unsupported result content type")
    if len(content) > MAX_RESULT_BYTES:
        raise ValueError("result is larger than the configured maximum")
    magic, extension = allowed[content_type]
    if not content.startswith(magic) or (extension == "webp" and content[8:12] != b"WEBP"):
        raise ValueError("result file header does not match content type")
    return content_type, len(content), hashlib.sha256(content).hexdigest()


def deterministic_object_key(task_id: UUID, attempt_id: UUID, content_type: str) -> str:
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]
    return f"results/{task_id}/{attempt_id}/0.{suffix}"


class MinioResultAssetStore:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        secure: bool | None = None,
        public_endpoint: str | None = None,
    ) -> None:
        raw_endpoint = endpoint or os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
        scheme, _, host = raw_endpoint.partition("://")
        self._client = Minio(
            host,
            access_key=access_key or os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=secret_key or os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            secure=(scheme == "https") if secure is None else secure,
            region=os.environ.get("MINIO_REGION", "us-east-1"),
        )
        self._bucket = bucket or os.environ.get("MINIO_BUCKET", "museflow-results")
        public_raw = public_endpoint or os.environ.get("MINIO_PUBLIC_ENDPOINT")
        self._signer = self._client
        if public_raw:
            public_scheme, _, public_host = public_raw.partition("://")
            self._signer = Minio(
                public_host,
                access_key=access_key or os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
                secret_key=secret_key or os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
                secure=public_scheme == "https",
                region=os.environ.get("MINIO_REGION", "us-east-1"),
            )

    def put_result(
        self,
        *,
        task_id: UUID,
        attempt_id: UUID,
        content: bytes,
        content_type: str,
    ) -> StoredAsset:
        content_type, size, sha256 = validate_result(content, content_type)
        key = deterministic_object_key(task_id, attempt_id, content_type)
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(content),
            size,
            content_type=content_type,
            metadata={"x-amz-meta-sha256": sha256},
        )
        return StoredAsset(key, content_type, size, sha256)

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
        return self._signer.presigned_get_object(
            self._bucket, object_key, expires=timedelta(seconds=expires_seconds)
        )

    def check_ready(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            raise RuntimeError("MinIO result bucket is not ready")


class NullResultAssetStore:
    """Compatibility seam for unit tests from the asynchronous slice.

    Production workers always inject MinioResultAssetStore.
    """

    def put_result(self, **_: object) -> StoredAsset:
        raise RuntimeError("result asset store is required for persisted results")

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
        raise RuntimeError("result asset store is required for downloads")

    def check_ready(self) -> None:
        return None
