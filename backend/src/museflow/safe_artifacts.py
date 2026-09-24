from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx
from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class SafeArtifactPolicy:
    allowed_hosts: frozenset[str]
    max_response_bytes: int
    allowed_content_types: frozenset[str]
    max_width: int
    max_height: int
    max_pixels: int
    max_frames: int = 1
    allowed_modes: frozenset[str] = frozenset({"RGB"})
    allow_alpha: bool = False
    max_redirects: int = 0


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    content: bytes
    content_type: str
    format: str
    width: int
    height: int
    frame_count: int
    mode: str
    size_bytes: int
    sha256: str
    host_sha256_12: str
    redirect_count: int


@dataclass(frozen=True, slots=True)
class _ArtifactResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes


class SafeArtifactError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(
        self,
        stream: httpcore.NetworkStream,
        *,
        deadline: float,
        monotonic: Callable[[], float],
        hostname: str,
    ) -> None:
        self._stream = stream
        self._deadline = deadline
        self._monotonic = monotonic
        self._hostname = hostname

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, self._bounded_timeout(timeout, "read"))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, self._bounded_timeout(timeout, "write"))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        if not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED:
            raise httpcore.ConnectError("TLS certificate verification must remain enabled")
        del server_hostname
        secured = self._stream.start_tls(
            ssl_context,
            server_hostname=self._hostname,
            timeout=self._bounded_timeout(timeout, "connect"),
        )
        return _DeadlineStream(
            secured,
            deadline=self._deadline,
            monotonic=self._monotonic,
            hostname=self._hostname,
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    def _bounded_timeout(self, timeout: float | None, operation: str) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            error_type = {
                "read": httpcore.ReadTimeout,
                "write": httpcore.WriteTimeout,
                "connect": httpcore.ConnectTimeout,
            }[operation]
            raise error_type("artifact transfer deadline exceeded")
        return remaining if timeout is None else min(timeout, remaining)


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect to the vetted address while keeping URL host and TLS identity intact."""

    def __init__(
        self,
        *,
        hostname: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        deadline: float,
        monotonic: Callable[[], float] = time.monotonic,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._hostname = hostname
        self._address = str(address)
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise ValueError("pinned network address must be globally routable")
        self._deadline = deadline
        self._monotonic = monotonic
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if host != self._hostname or port != 443:
            raise httpcore.ConnectError("connection origin does not match validated artifact host")
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout("artifact connection deadline exceeded")
        bounded = remaining if timeout is None else min(timeout, remaining)
        stream = self._backend.connect_tcp(
            host=self._address,
            port=port,
            timeout=bounded,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _DeadlineStream(
            stream,
            deadline=self._deadline,
            monotonic=self._monotonic,
            hostname=self._hostname,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("Unix socket connections are disabled for artifacts")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _HttpcoreResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


class PinnedHttpxTransport(httpx.BaseTransport):
    """Small HTTPX adapter over httpcore with a per-request pinned TCP backend."""

    def __init__(
        self,
        *,
        hostname: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        deadline: float,
        monotonic: Callable[[], float] = time.monotonic,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self.hostname = hostname
        self.ssl_context = ssl.create_default_context()
        self.network_backend = PinnedNetworkBackend(
            hostname=hostname,
            address=address,
            deadline=deadline,
            monotonic=monotonic,
            backend=backend,
        )
        self._pool = httpcore.ConnectionPool(
            ssl_context=self.ssl_context,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            retries=0,
            network_backend=self.network_backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host.lower() != self.hostname
            or request.url.port not in (None, 443)
        ):
            raise httpx.NetworkError(
                "request origin does not match pinned artifact context", request=request
            )
        extensions: dict[str, Any] = dict(request.extensions)
        extensions.pop("sni_hostname", None)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=cast(Iterable[bytes], request.stream),
            extensions=extensions,
        )
        try:
            core_response = self._pool.handle_request(core_request)
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError):
            raise httpx.NetworkError("pinned artifact connection failed", request=request) from None
        raw_extensions: Any = getattr(core_response, "extensions", {})
        response_extensions: dict[str, Any] = dict(raw_extensions)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_HttpcoreResponseStream(cast(Iterable[bytes], core_response.stream)),
            extensions=response_extensions,
            request=request,
        )

    def close(self) -> None:
        self._pool.close()


Resolver = Callable[[str], list[str]]
TransportFactory = Callable[
    [str, ipaddress.IPv4Address | ipaddress.IPv6Address, float], httpx.BaseTransport
]


def resolve_host(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise SafeArtifactError(
            "RESULT_FETCH_UNAVAILABLE", "artifact host could not be resolved", retryable=True
        ) from error
    return list(dict.fromkeys(str(record[4][0]) for record in records))


class SafeArtifactFetcher:
    """Fetch one untrusted result URL using strict URL, network, size, and image checks."""

    def __init__(
        self,
        *,
        resolver: Resolver = resolve_host,
        transport_factory: TransportFactory | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        write_timeout: float = 5.0,
        pool_timeout: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._resolver = resolver
        self._transport_factory = transport_factory
        self._timeouts = (connect_timeout, read_timeout, write_timeout, pool_timeout)
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    def fetch(
        self,
        url: str,
        *,
        policy: SafeArtifactPolicy,
        deadline_at: datetime | None = None,
        timeout_seconds: float = 600.0,
        ownership_check: Callable[[], None] | None = None,
    ) -> VerifiedArtifact:
        self._validate_policy(policy)
        deadline = self._make_deadline(deadline_at, timeout_seconds)
        current_url = url
        visited: set[str] = set()
        redirects = 0
        while True:
            normalized_url, hostname = self._validate_url(current_url, policy.allowed_hosts)
            self._check_ownership(ownership_check)
            if normalized_url in visited:
                raise SafeArtifactError(
                    "RESULT_URL_UNSAFE", "artifact redirect loop was rejected", retryable=False
                )
            visited.add(normalized_url)
            addresses = self._validated_addresses(hostname, ownership_check=ownership_check)
            response = self._request(
                normalized_url,
                hostname=hostname,
                address=addresses[0],
                deadline=deadline,
                policy=policy,
                ownership_check=ownership_check,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirects >= policy.max_redirects:
                    raise SafeArtifactError(
                        "RESULT_REDIRECT_UNSAFE",
                        "artifact redirect was rejected",
                        retryable=False,
                    )
                current_url = urljoin(normalized_url, location)
                redirects += 1
                continue
            if response.status_code != 200:
                retryable = response.status_code == 429 or response.status_code >= 500
                raise SafeArtifactError(
                    "RESULT_FETCH_UNAVAILABLE" if retryable else "RESULT_DOWNLOAD_FAILED",
                    "artifact server returned an unsuccessful response",
                    retryable=retryable,
                )
            content_type = self._content_type(response.headers, policy)
            return self._verify_image(
                response.content,
                content_type=content_type,
                hostname=hostname,
                redirect_count=redirects,
                policy=policy,
                deadline=deadline,
                ownership_check=ownership_check,
            )

    def _request(
        self,
        url: str,
        *,
        hostname: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        deadline: float,
        policy: SafeArtifactPolicy,
        ownership_check: Callable[[], None] | None,
    ) -> _ArtifactResponse:
        remaining = self._remaining(deadline)
        transport = (
            self._transport_factory(hostname, address, deadline)
            if self._transport_factory is not None
            else PinnedHttpxTransport(
                hostname=hostname,
                address=address,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        )
        connect, read, write, pool = self._timeouts
        timeout = httpx.Timeout(
            connect=min(connect, remaining),
            read=min(read, remaining),
            write=min(write, remaining),
            pool=min(pool, remaining),
        )
        client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )
        try:
            with client.stream("GET", url) as streamed:
                if self._remaining(deadline) <= 0:
                    raise SafeArtifactError(
                        "RESULT_FETCH_DEADLINE", "artifact fetch deadline exceeded", retryable=True
                    )
                headers = {key.lower(): value for key, value in streamed.headers.items()}
                if streamed.status_code in {301, 302, 303, 307, 308}:
                    return _ArtifactResponse(streamed.status_code, headers, b"")
                if streamed.status_code != 200:
                    return _ArtifactResponse(streamed.status_code, headers, b"")
                self._content_type(streamed.headers, policy)
                self._check_content_length(streamed.headers, policy.max_response_bytes)
                if streamed.headers.get("content-encoding", "identity").lower() != "identity":
                    raise SafeArtifactError(
                        "RESULT_INVALID",
                        "compressed artifact responses are not accepted",
                        retryable=False,
                    )
                content = self._read_bounded(
                    streamed,
                    policy.max_response_bytes,
                    deadline,
                    ownership_check=ownership_check,
                )
                return _ArtifactResponse(streamed.status_code, headers, content)
        except SafeArtifactError:
            raise
        except httpx.TransportError:
            raise SafeArtifactError(
                "RESULT_FETCH_UNAVAILABLE", "artifact transfer failed", retryable=True
            ) from None
        finally:
            client.close()

    def _validated_addresses(
        self,
        hostname: str,
        *,
        ownership_check: Callable[[], None] | None,
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        self._check_ownership(ownership_check)
        try:
            raw_addresses = self._resolver(hostname)
        except SafeArtifactError:
            raise
        except OSError:
            raise SafeArtifactError(
                "RESULT_FETCH_UNAVAILABLE", "artifact host could not be resolved", retryable=True
            ) from None
        if not raw_addresses:
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact host has no addresses", retryable=False
            )
        self._check_ownership(ownership_check)
        parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        try:
            for raw_address in raw_addresses:
                if "%" in raw_address:
                    raise ValueError("scoped addresses are forbidden")
                address = ipaddress.ip_address(raw_address)
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                    address = address.ipv4_mapped
                if (
                    not address.is_global
                    or address.is_loopback
                    or address.is_private
                    or address.is_link_local
                    or address.is_multicast
                    or address.is_unspecified
                    or address.is_reserved
                ):
                    raise ValueError("non-public address")
                parsed.append(address)
        except ValueError:
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE",
                "artifact host resolved to a forbidden address",
                retryable=False,
            ) from None
        return list(dict.fromkeys(parsed))

    def _read_bounded(
        self,
        response: httpx.Response,
        maximum: int,
        deadline: float,
        *,
        ownership_check: Callable[[], None] | None,
    ) -> bytes:
        if response.is_stream_consumed:
            self._check_ownership(ownership_check)
            if self._remaining(deadline) <= 0:
                raise SafeArtifactError(
                    "RESULT_FETCH_DEADLINE", "artifact fetch deadline exceeded", retryable=True
                )
            content = response.content
            if len(content) > maximum:
                raise SafeArtifactError(
                    "RESULT_INVALID", "artifact exceeds its byte limit", retryable=False
                )
            return content
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_raw():
                self._check_ownership(ownership_check)
                if self._remaining(deadline) <= 0:
                    raise SafeArtifactError(
                        "RESULT_FETCH_DEADLINE", "artifact fetch deadline exceeded", retryable=True
                    )
                total += len(chunk)
                if total > maximum:
                    raise SafeArtifactError(
                        "RESULT_INVALID", "artifact exceeds its byte limit", retryable=False
                    )
                chunks.append(chunk)
        except SafeArtifactError:
            raise
        except httpx.TransportError:
            raise SafeArtifactError(
                "RESULT_FETCH_UNAVAILABLE", "artifact stream failed", retryable=True
            ) from None
        return b"".join(chunks)

    @staticmethod
    def _content_type(headers: Mapping[str, str], policy: SafeArtifactPolicy) -> str:
        value = headers.get("content-type", "")
        content_type = value.split(";", 1)[0].strip().lower()
        if content_type not in policy.allowed_content_types:
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact media type is not allowed", retryable=False
            )
        return content_type

    @staticmethod
    def _check_content_length(headers: Mapping[str, str], maximum: int) -> None:
        value = headers.get("content-length")
        if value is None:
            return
        if not value.isascii() or not value.isdecimal():
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact content length is invalid", retryable=False
            )
        if int(value) > maximum:
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact exceeds its byte limit", retryable=False
            )

    def _verify_image(
        self,
        content: bytes,
        *,
        content_type: str,
        hostname: str,
        redirect_count: int,
        policy: SafeArtifactPolicy,
        deadline: float,
        ownership_check: Callable[[], None] | None,
    ) -> VerifiedArtifact:
        signatures = {
            "image/png": (b"\x89PNG\r\n\x1a\n", "PNG"),
            "image/jpeg": (b"\xff\xd8\xff", "JPEG"),
            "image/webp": (b"RIFF", "WEBP"),
        }
        signature = signatures.get(content_type)
        if signature is None or not content.startswith(signature[0]):
            raise SafeArtifactError(
                "RESULT_INVALID",
                "artifact signature does not match its media type",
                retryable=False,
            )
        if content_type == "image/webp" and content[8:12] != b"WEBP":
            raise SafeArtifactError(
                "RESULT_INVALID",
                "artifact signature does not match its media type",
                retryable=False,
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    image_format = image.format
                    width, height = image.size
                    frame_count = getattr(image, "n_frames", 1)
                    mode = image.mode
                    has_alpha = "A" in image.getbands() or "transparency" in image.info
                    self._validate_image_metadata(
                        width,
                        height,
                        frame_count,
                        mode,
                        has_alpha,
                        signature[1],
                        policy,
                    )
                    if image_format != signature[1]:
                        raise ValueError("artifact format does not match its signature")
                    image.verify()
                with Image.open(BytesIO(content)) as image:
                    image.load()
                    if (
                        image.size != (width, height)
                        or image.mode != mode
                        or image.format != signature[1]
                    ):
                        raise ValueError("decoded image metadata changed")
        except SafeArtifactError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact exceeded decoder resource limits", retryable=False
            ) from None
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError):
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact could not be fully decoded", retryable=False
            ) from None
        if self._remaining(deadline) <= 0:
            raise SafeArtifactError(
                "RESULT_FETCH_DEADLINE", "artifact validation deadline exceeded", retryable=True
            )
        self._check_ownership(ownership_check)
        return VerifiedArtifact(
            content=content,
            content_type=content_type,
            format=image_format or signature[1],
            width=width,
            height=height,
            frame_count=frame_count,
            mode=mode,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            host_sha256_12=hashlib.sha256(hostname.encode("ascii")).hexdigest()[:12],
            redirect_count=redirect_count,
        )

    @staticmethod
    def _validate_image_metadata(
        width: int,
        height: int,
        frame_count: int,
        mode: str,
        has_alpha: bool,
        expected_format: str,
        policy: SafeArtifactPolicy,
    ) -> None:
        if expected_format not in {"PNG", "JPEG", "WEBP"}:
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact format is not allowed by this profile", retryable=False
            )
        if frame_count > policy.max_frames:
            raise SafeArtifactError(
                "RESULT_INVALID", "animated artifacts are not allowed", retryable=False
            )
        if mode not in policy.allowed_modes or (has_alpha and not policy.allow_alpha):
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact color mode is not allowed", retryable=False
            )
        if (
            width <= 0
            or height <= 0
            or width > policy.max_width
            or height > policy.max_height
            or width * height > policy.max_pixels
        ):
            raise SafeArtifactError(
                "RESULT_INVALID", "artifact dimensions exceed profile limits", retryable=False
            )

    def _validate_url(self, url: str, allowed_hosts: frozenset[str]) -> tuple[str, str]:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact URL is malformed", retryable=False
            ) from None
        if (
            parsed.scheme.lower() != "https"
            or hostname is None
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or any(
                character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
                for character in url
            )
        ):
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact URL is not allowed", retryable=False
            )
        try:
            normalized_host = self._normalize_hostname(hostname)
        except ValueError:
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact URL host is malformed", retryable=False
            ) from None
        if self._is_ip_literal(normalized_host):
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact IP literals are not allowed", retryable=False
            )
        try:
            normalized_allowlist = {self._normalize_hostname(host) for host in allowed_hosts}
        except ValueError:
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact host policy is invalid", retryable=False
            ) from None
        if normalized_host not in normalized_allowlist:
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact host is not allowlisted", retryable=False
            )
        netloc = normalized_host
        if port == 443:
            netloc += ":443"
        normalized_url = urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))
        return normalized_url, normalized_host

    @staticmethod
    def _normalize_hostname(hostname: str) -> str:
        if "%" in hostname or not hostname:
            raise ValueError("invalid host")
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not normalized or len(normalized) > 253:
            raise ValueError("invalid host")
        return normalized

    @staticmethod
    def _is_ip_literal(hostname: str) -> bool:
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return False

    def _make_deadline(self, deadline_at: datetime | None, timeout_seconds: float) -> float:
        if timeout_seconds <= 0:
            raise SafeArtifactError(
                "RESULT_FETCH_DEADLINE", "artifact deadline exceeded", retryable=True
            )
        remaining = timeout_seconds
        if deadline_at is not None:
            if deadline_at.tzinfo is None:
                raise SafeArtifactError(
                    "RESULT_FETCH_DEADLINE", "artifact deadline is invalid", retryable=False
                )
            remaining = min(remaining, (deadline_at - self._wall_clock()).total_seconds())
        if remaining <= 0:
            raise SafeArtifactError(
                "RESULT_FETCH_DEADLINE", "artifact deadline exceeded", retryable=True
            )
        return self._monotonic() + remaining

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise SafeArtifactError(
                "RESULT_FETCH_DEADLINE", "artifact deadline exceeded", retryable=True
            )
        return remaining

    @staticmethod
    def _check_ownership(ownership_check: Callable[[], None] | None) -> None:
        if ownership_check is not None:
            ownership_check()

    @staticmethod
    def _validate_policy(policy: SafeArtifactPolicy) -> None:
        if (
            not policy.allowed_hosts
            or policy.max_response_bytes <= 0
            or not policy.allowed_content_types
            or policy.max_width <= 0
            or policy.max_height <= 0
            or policy.max_pixels <= 0
            or policy.max_frames <= 0
            or policy.max_redirects < 0
            or policy.max_redirects > 2
            or any("*" in host for host in policy.allowed_hosts)
            or any(SafeArtifactFetcher._is_ip_literal(host) for host in policy.allowed_hosts)
        ):
            raise SafeArtifactError(
                "RESULT_URL_UNSAFE", "artifact policy is invalid", retryable=False
            )
