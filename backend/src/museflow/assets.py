from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from minio import Minio

MAX_RESULT_BYTES = 20 * 1024 * 1024
DEFAULT_RESULT_HOSTS = frozenset({"dashscope-result-bj.oss-cn-beijing.aliyuncs.com"})


@dataclass(frozen=True, slots=True)
class StoredAsset:
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DownloadedResult:
    content: bytes
    content_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    status_code: int


@dataclass(frozen=True, slots=True)
class ResultHostDiagnostic:
    allowlisted: bool
    host_digest: str | None

    @property
    def summary(self) -> str:
        state = "yes" if self.allowlisted else "no"
        digest = self.host_digest or "unavailable"
        return f"allowlisted={state}, host_digest={digest}"


class ResultDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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


def _image_dimensions(content: bytes, content_type: str) -> tuple[int, int]:
    if content_type == "image/png":
        if len(content) < 24 or content[12:16] != b"IHDR":
            raise ValueError("result PNG header is incomplete")
        return struct.unpack(">II", content[16:24])

    if content_type == "image/webp":
        if len(content) < 30:
            raise ValueError("result WebP header is incomplete")
        if content[12:16] == b"VP8X":
            return (
                1 + int.from_bytes(content[24:27], "little"),
                1 + int.from_bytes(content[27:30], "little"),
            )
        raise ValueError("result WebP dimensions are unsupported")

    if content_type == "image/jpeg":
        position = 2
        while position + 4 <= len(content):
            if content[position] != 0xFF:
                position += 1
                continue
            marker = content[position + 1]
            position += 2
            if marker in {0xD8, 0xD9}:
                continue
            if position + 2 > len(content):
                break
            segment_length = int.from_bytes(content[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(content):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if segment_length < 7:
                    break
                return (
                    int.from_bytes(content[position + 5 : position + 7], "big"),
                    int.from_bytes(content[position + 3 : position + 5], "big"),
                )
            position += segment_length
        raise ValueError("result JPEG dimensions are missing")

    raise ValueError("unsupported result content type")


def _default_resolve_host(host: str) -> list[str]:
    return [str(address[4][0]) for address in socket.getaddrinfo(host, None)]


def _assert_safe_host(host: str, resolve_host: Callable[[str], list[str]]) -> None:
    try:
        addresses = [ipaddress.ip_address(address) for address in resolve_host(host)]
    except (OSError, ValueError) as error:
        raise ValueError("result host could not be resolved safely") from error
    if not addresses or any(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    ):
        raise ValueError("result host resolves to a forbidden network")


class SecureResultDownloader:
    """Download a Provider result without allowing URL-based network escapes."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        allowed_hosts: frozenset[str] = DEFAULT_RESULT_HOSTS,
        resolve_host: Callable[[str], list[str]] = _default_resolve_host,
        expected_dimensions: tuple[int, int] | None = None,
        max_redirects: int = 2,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            follow_redirects=False,
        )
        self._allowed_hosts = allowed_hosts
        self._resolve_host = resolve_host
        self._expected_dimensions = expected_dimensions
        self._max_redirects = max_redirects

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def diagnose_url(self, url: str) -> ResultHostDiagnostic:
        try:
            host = urlparse(url).hostname
        except ValueError:
            return ResultHostDiagnostic(False, None)
        if not host:
            return ResultHostDiagnostic(False, None)
        normalized_host = host.lower()
        digest = hashlib.sha256(normalized_host.encode("utf-8")).hexdigest()[:12]
        return ResultHostDiagnostic(
            normalized_host in self._allowed_hosts,
            digest,
        )

    def download(self, url: str) -> DownloadedResult:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            self._validate_url(current_url)
            request = self._client.build_request("GET", current_url)
            response = self._client.send(request, stream=True)
            try:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location or redirect_count >= self._max_redirects:
                        raise ValueError("result redirect is not allowed")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code != 200:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    raise ResultDownloadError(
                        "RESULT_DOWNLOAD_UNAVAILABLE" if retryable else "RESULT_DOWNLOAD_FAILED",
                        f"result download returned HTTP {response.status_code}",
                        retryable=retryable,
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ValueError("result content type is not supported")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_RESULT_BYTES:
                            raise ValueError("result is larger than the configured maximum")
                    except ValueError as error:
                        if "larger" in str(error):
                            raise
                        raise ValueError("result content length is invalid") from error

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESULT_BYTES:
                        raise ValueError("result is larger than the configured maximum")
                    chunks.append(chunk)
                content = b"".join(chunks)
                validate_result(content, content_type)
                width, height = _image_dimensions(content, content_type)
                if self._expected_dimensions and (width, height) != self._expected_dimensions:
                    raise ValueError("result dimensions do not match the configured size")
                _, size, sha256 = validate_result(content, content_type)
                return DownloadedResult(
                    content=content,
                    content_type=content_type,
                    width=width,
                    height=height,
                    size_bytes=size,
                    sha256=sha256,
                    status_code=response.status_code,
                )
            finally:
                response.close()
        raise ValueError("result redirect limit exceeded")

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
        except ValueError as error:
            raise ValueError("result URL is not allowed") from error
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("result URL is not allowed")
        host = parsed.hostname.lower()
        diagnostic = self.diagnose_url(url)
        if not diagnostic.allowlisted:
            raise ValueError("result host is not allowed")
        _assert_safe_host(host, self._resolve_host)


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
