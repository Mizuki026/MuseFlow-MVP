from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, Protocol

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Timeout  # pyright: ignore[reportMissingTypeStubs]

MAX_BLOB_READ_BYTES = 6_000_000
_BLOB_CHUNK_BYTES = 64 * 1024
_OBJECT_STORE_CONNECT_TIMEOUT = 5.0
_OBJECT_STORE_READ_TIMEOUT = 30.0


def _bounded_http_pool() -> PoolManager:
    return PoolManager(
        timeout=Timeout(connect=_OBJECT_STORE_CONNECT_TIMEOUT, read=_OBJECT_STORE_READ_TIMEOUT),
        retries=False,
    )


class BlobNotFoundError(FileNotFoundError):
    pass


class BlobObjectTooLargeError(ValueError):
    pass


class BlobStoreUnavailable(RuntimeError):
    """A storage failure with no endpoint, key, or credential in its message."""


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    size_bytes: int
    content_type: str | None
    sha256: str | None


class BlobStore(Protocol):
    def put(
        self,
        object_key: str,
        stream: BinaryIO,
        *,
        size_bytes: int,
        content_type: str,
        sha256: str,
    ) -> None: ...

    def get(self, object_key: str, *, max_bytes: int = MAX_BLOB_READ_BYTES) -> bytes: ...

    def stat(self, object_key: str) -> BlobMetadata: ...

    def delete(self, object_key: str) -> None: ...

    def presigned_get(self, object_key: str, *, expires_seconds: int = 300) -> str: ...


class MinioBlobStore:
    """Private MinIO adapter for reference image objects."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        public_endpoint: str | None = None,
    ) -> None:
        raw_endpoint = endpoint or os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
        scheme, _, host = raw_endpoint.partition("://")
        key = access_key or os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
        secret = secret_key or os.environ.get("MINIO_SECRET_KEY", "minioadmin")
        self._client = Minio(
            host,
            access_key=key,
            secret_key=secret,
            secure=scheme == "https",
            region=os.environ.get("MINIO_REGION", "us-east-1"),
            http_client=_bounded_http_pool(),
        )
        self._bucket = bucket or os.environ.get("MINIO_BUCKET", "museflow-results")
        signer = public_endpoint or os.environ.get("MINIO_PUBLIC_ENDPOINT")
        if signer:
            public_scheme, _, public_host = signer.partition("://")
            self._signer = Minio(
                public_host,
                access_key=key,
                secret_key=secret,
                secure=public_scheme == "https",
                region=os.environ.get("MINIO_REGION", "us-east-1"),
                http_client=_bounded_http_pool(),
            )
        else:
            self._signer = self._client

    def put(
        self,
        object_key: str,
        stream: BinaryIO,
        *,
        size_bytes: int,
        content_type: str,
        sha256: str,
    ) -> None:
        try:
            self._client.put_object(
                self._bucket,
                object_key,
                stream,
                size_bytes,
                content_type=content_type,
                metadata={"sha256": sha256},
            )
        except Exception:
            raise BlobStoreUnavailable("object storage write failed") from None

    def get(self, object_key: str, *, max_bytes: int = MAX_BLOB_READ_BYTES) -> bytes:
        response = None
        try:
            response = self._client.get_object(self._bucket, object_key)
            chunks: list[bytes] = []
            size_bytes = 0
            while chunk := response.read(_BLOB_CHUNK_BYTES):
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise BlobObjectTooLargeError("stored object exceeds the safe read limit")
                chunks.append(chunk)
            return b"".join(chunks)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise BlobNotFoundError("object does not exist") from None
            raise BlobStoreUnavailable("object storage read failed") from None
        except BlobObjectTooLargeError:
            raise
        except Exception:
            raise BlobStoreUnavailable("object storage read failed") from None
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def stat(self, object_key: str) -> BlobMetadata:
        try:
            result = self._client.stat_object(self._bucket, object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise BlobNotFoundError("object does not exist") from None
            raise BlobStoreUnavailable("object storage metadata is unavailable") from None
        except Exception:
            raise BlobStoreUnavailable("object storage metadata is unavailable") from None
        metadata = {key.lower(): value for key, value in (result.metadata or {}).items()}
        if result.size is None:
            raise BlobStoreUnavailable("object storage metadata is unavailable")
        return BlobMetadata(
            size_bytes=result.size,
            content_type=result.content_type,
            sha256=metadata.get("x-amz-meta-sha256") or metadata.get("sha256"),
        )

    def delete(self, object_key: str) -> None:
        try:
            self._client.remove_object(self._bucket, object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return
            raise BlobStoreUnavailable("object storage delete failed") from None
        except Exception:
            raise BlobStoreUnavailable("object storage delete failed") from None

    def presigned_get(self, object_key: str, *, expires_seconds: int = 300) -> str:
        try:
            return self._signer.presigned_get_object(
                self._bucket, object_key, expires=timedelta(seconds=expires_seconds)
            )
        except Exception:
            raise BlobStoreUnavailable("object access could not be prepared") from None

    def check_ready(self) -> None:
        try:
            if not self._client.bucket_exists(self._bucket):
                raise BlobStoreUnavailable("object storage is not ready")
        except BlobStoreUnavailable:
            raise
        except Exception:
            raise BlobStoreUnavailable("object storage is not ready") from None
