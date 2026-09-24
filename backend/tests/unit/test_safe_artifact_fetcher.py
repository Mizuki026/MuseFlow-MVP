from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpcore
import httpx
import pytest
from PIL import Image

from museflow.safe_artifacts import (
    PinnedHttpxTransport,
    PinnedNetworkBackend,
    SafeArtifactError,
    SafeArtifactFetcher,
    SafeArtifactPolicy,
)
from museflow.tasks.lease_guard import OwnershipLostError

RESULT_HOST = "artifact.example.test"
PUBLIC_IP = "93.184.216.34"
RESULT_BYTES = 64 * 1024


def _image_bytes(
    *, size: tuple[int, int] = (32, 24), mode: str = "RGB", format: str = "PNG"
) -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, (12, 42, 90) if mode == "RGB" else 70).save(output, format=format)
    return output.getvalue()


def _policy(
    *,
    hosts: frozenset[str] = frozenset({RESULT_HOST}),
    max_bytes: int = RESULT_BYTES,
    max_redirects: int = 0,
    media: frozenset[str] = frozenset({"image/png"}),
    max_width: int = 128,
    max_height: int = 128,
    max_pixels: int = 16_384,
    modes: frozenset[str] = frozenset({"RGB"}),
    allow_alpha: bool = False,
) -> SafeArtifactPolicy:
    return SafeArtifactPolicy(
        allowed_hosts=hosts,
        max_response_bytes=max_bytes,
        allowed_content_types=media,
        max_width=max_width,
        max_height=max_height,
        max_pixels=max_pixels,
        max_frames=1,
        allowed_modes=modes,
        allow_alpha=allow_alpha,
        max_redirects=max_redirects,
    )


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    addresses: list[str] | None = None,
    transport_factory: Callable[
        [str, ipaddress.IPv4Address | ipaddress.IPv6Address, float], httpx.BaseTransport
    ]
    | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SafeArtifactFetcher:
    return SafeArtifactFetcher(
        resolver=lambda _: addresses or [PUBLIC_IP],
        transport_factory=transport_factory
        or (lambda _host, _ip, _deadline: httpx.MockTransport(handler)),
        monotonic=monotonic or time.monotonic,
    )


def test_valid_https_artifact_is_fully_decoded_and_summarized() -> None:
    content = _image_bytes()
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=content)
    )

    verified = fetcher.fetch(f"https://{RESULT_HOST}/image.png#never-send", policy=_policy())

    assert verified.content == content
    assert verified.content_type == "image/png"
    assert (verified.format, verified.width, verified.height) == ("PNG", 32, 24)
    assert (verified.frame_count, verified.mode, verified.size_bytes) == (1, "RGB", len(content))
    assert verified.sha256 == hashlib.sha256(content).hexdigest()
    assert verified.redirect_count == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://artifact.example.test/image.png",
        "https:///image.png",
        "https://user:pass@artifact.example.test/image.png",
        "https://artifact.example.test:444/image.png",
        "https://93.184.216.34/image.png",
        "https://artifact.example.test.evil.test/image.png",
        "https://artifact.example.test/image with spaces.png",
        "https://artifact.example.test/image\x7f.png",
        "https://artifact.example.test/image\x85.png",
        "not a url",
    ],
)
def test_unsafe_url_shapes_fail_before_dns_or_http(url: str) -> None:
    requested: list[httpx.Request] = []
    fetcher = SafeArtifactFetcher(
        resolver=lambda _: pytest.fail("invalid URL reached DNS"),
        transport_factory=lambda *_: httpx.MockTransport(
            lambda request: requested.append(request) or httpx.Response(200)
        ),
    )

    with pytest.raises(SafeArtifactError) as error:
        fetcher.fetch(url, policy=_policy())

    assert error.value.code == "RESULT_URL_UNSAFE"
    assert requested == []


def test_host_normalization_is_exact_and_fragment_is_not_sent() -> None:
    requests: list[httpx.Request] = []
    content = _image_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=content)

    fetcher = _fetcher(handler)
    fetcher.fetch(f"https://{RESULT_HOST.upper()}./image.png#fragment", policy=_policy())

    assert requests[0].url.host == RESULT_HOST
    assert requests[0].url.fragment == ""
    assert requests[0].headers["host"] == RESULT_HOST


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.2",
        "169.254.1.2",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.2",
        "240.0.0.1",
        "::ffff:127.0.0.1",
        "fe80::1%12",
    ],
)
def test_forbidden_dns_address_classes_are_rejected(address: str) -> None:
    fetcher = _fetcher(
        lambda _: pytest.fail("forbidden IP reached the HTTP transport"), addresses=[address]
    )

    with pytest.raises(SafeArtifactError) as error:
        fetcher.fetch(f"https://{RESULT_HOST}/image.png", policy=_policy())

    assert error.value.code == "RESULT_URL_UNSAFE"


def test_mixed_public_and_private_dns_answer_is_rejected_as_a_whole() -> None:
    fetcher = _fetcher(
        lambda _: pytest.fail("mixed DNS answer reached the HTTP transport"),
        addresses=[PUBLIC_IP, "10.0.0.7"],
    )

    with pytest.raises(SafeArtifactError, match="forbidden address"):
        fetcher.fetch(f"https://{RESULT_HOST}/image.png", policy=_policy())


def test_dns_rebinding_does_not_change_the_selected_socket_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[str, int]] = []

    class FakeSocket:
        def setsockopt(self, *_: object) -> None:
            pass

        def close(self) -> None:
            pass

    def rebound_lookup(host: str, *_: object, **__: object) -> list[Any]:
        if host == RESULT_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return []

    def capture_connection(address: tuple[str, int], *_: object, **__: object) -> FakeSocket:
        connected.append(address)
        return FakeSocket()

    monkeypatch.setattr(socket, "getaddrinfo", rebound_lookup)
    monkeypatch.setattr(socket, "create_connection", capture_connection)
    pinned = PinnedNetworkBackend(
        hostname=RESULT_HOST,
        address=ipaddress.ip_address(PUBLIC_IP),
        deadline=time.monotonic() + 5,
    )

    pinned.connect_tcp(RESULT_HOST, 443, timeout=1)

    assert connected == [(PUBLIC_IP, 443)]


def test_pinned_tls_uses_original_sni_and_verified_certificate_context() -> None:
    observed: dict[str, object] = {}

    class RecordingStream(httpcore.NetworkStream):
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            raise NotImplementedError

        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            raise NotImplementedError

        def close(self) -> None:
            pass

        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore.NetworkStream:
            observed["sni"] = server_hostname
            observed["check_hostname"] = ssl_context.check_hostname
            observed["verify_mode"] = ssl_context.verify_mode
            return self

        def get_extra_info(self, info: str) -> Any:
            return None

    class RecordingBackend(httpcore.NetworkBackend):
        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.NetworkStream:
            observed["connect_host"] = host
            return RecordingStream()

        def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
            raise AssertionError("Unix socket path is disabled")

    transport = PinnedHttpxTransport(
        hostname=RESULT_HOST,
        address=ipaddress.ip_address(PUBLIC_IP),
        deadline=time.monotonic() + 5,
        backend=RecordingBackend(),
    )
    stream = transport.network_backend.connect_tcp(RESULT_HOST, 443, timeout=1)
    stream.start_tls(transport.ssl_context, server_hostname="attacker.example", timeout=1)
    transport.close()

    assert observed["connect_host"] == PUBLIC_IP
    assert observed["sni"] == RESULT_HOST
    assert observed["check_hostname"] is True
    assert observed["verify_mode"] == ssl.CERT_REQUIRED


def test_redirect_is_revalidated_and_only_explicitly_allowed_host_can_be_followed() -> None:
    second_host = "artifact-2.example.test"
    requests: list[str] = []
    content = _image_bytes()

    def factory(host: str, _ip: ipaddress.IPv4Address | ipaddress.IPv6Address, _deadline: float):
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if host == RESULT_HOST:
                return httpx.Response(
                    302, headers={"location": f"https://{second_host}/result.png#fragment"}
                )
            return httpx.Response(200, headers={"content-type": "image/png"}, content=content)

        return httpx.MockTransport(handler)

    fetcher = SafeArtifactFetcher(resolver=lambda _: [PUBLIC_IP], transport_factory=factory)
    verified = fetcher.fetch(
        f"https://{RESULT_HOST}/start",
        policy=_policy(hosts=frozenset({RESULT_HOST, second_host}), max_redirects=1),
    )

    assert verified.redirect_count == 1
    assert len(requests) == 2
    assert "#fragment" not in requests[1]


@pytest.mark.parametrize(
    "location",
    [
        "https://not-allowed.example/result.png",
        "http://artifact.example.test/result.png",
        "https://artifact.example.test:444/result.png",
    ],
)
def test_unsafe_redirect_target_fails_closed(location: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    fetcher = _fetcher(handler)
    with pytest.raises(SafeArtifactError):
        fetcher.fetch(
            f"https://{RESULT_HOST}/start",
            policy=_policy(max_redirects=2),
        )
    assert len(requests) == 1


def test_redirect_loop_and_redirect_limit_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    fetcher = _fetcher(handler)
    with pytest.raises(SafeArtifactError, match="redirect loop"):
        fetcher.fetch(
            f"https://{RESULT_HOST}/loop",
            policy=_policy(max_redirects=2),
        )

    second_host = "artifact-2.example.test"
    request_count = 0

    def redirects_twice(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        next_host = second_host if request_count == 1 else RESULT_HOST
        return httpx.Response(302, headers={"location": f"https://{next_host}/next"})

    bounded_fetcher = SafeArtifactFetcher(
        resolver=lambda _: [PUBLIC_IP],
        transport_factory=lambda *_: httpx.MockTransport(redirects_twice),
    )
    with pytest.raises(SafeArtifactError, match="redirect was rejected"):
        bounded_fetcher.fetch(
            f"https://{RESULT_HOST}/start",
            policy=_policy(hosts=frozenset({RESULT_HOST, second_host}), max_redirects=1),
        )
    assert request_count == 2


@pytest.mark.parametrize("declared", [str(RESULT_BYTES + 1), "invalid", "-1"])
def test_untrusted_content_length_is_checked_before_body_read(declared: str) -> None:
    read_called = False

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal read_called
            read_called = True
            yield _image_bytes()

        def close(self) -> None:
            pass

    fetcher = _fetcher(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": declared},
            stream=Stream(),
        )
    )
    with pytest.raises(SafeArtifactError):
        fetcher.fetch(f"https://{RESULT_HOST}/image.png", policy=_policy())
    assert read_called is False


@pytest.mark.parametrize("with_content_length", [False, True])
def test_actual_stream_size_limit_wins_even_if_length_is_missing_or_too_small(
    with_content_length: bool,
) -> None:
    actual = _image_bytes() + b"x" * 2048
    headers = {"content-type": "image/png"}
    if with_content_length:
        headers["content-length"] = "1"

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield actual[:100]
            yield actual[100:]

        def close(self) -> None:
            pass

    fetcher = _fetcher(lambda _: httpx.Response(200, headers=headers, stream=Chunks()))

    with pytest.raises(SafeArtifactError, match="byte limit"):
        fetcher.fetch(
            f"https://{RESULT_HOST}/image.png", policy=_policy(max_bytes=len(_image_bytes()))
        )


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("text/plain", _image_bytes()),
        ("image/png", b"not a png"),
        ("image/png", _image_bytes()[:-12]),
        ("image/gif", b"GIF89a"),
    ],
)
def test_wrong_mime_signature_or_truncated_media_is_rejected(
    content_type: str, content: bytes
) -> None:
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": content_type}, content=content)
    )
    with pytest.raises(SafeArtifactError):
        fetcher.fetch(f"https://{RESULT_HOST}/image", policy=_policy())


@pytest.mark.parametrize(
    ("content", "policy"),
    [
        (_image_bytes(mode="RGBA"), _policy()),
        (_image_bytes(mode="L"), _policy()),
        (_image_bytes(size=(129, 10)), _policy()),
        (_image_bytes(size=(128, 128)), _policy(max_pixels=100)),
        (_image_bytes(format="JPEG"), _policy()),
    ],
)
def test_alpha_mode_format_and_dimensions_fail_profile_validation(
    content: bytes, policy: SafeArtifactPolicy
) -> None:
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=content)
    )
    with pytest.raises(SafeArtifactError):
        fetcher.fetch(f"https://{RESULT_HOST}/image", policy=policy)


def test_decompression_bomb_warning_is_treated_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    content = _image_bytes(size=(32, 24))
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=content)
    )

    with pytest.raises(SafeArtifactError, match="decoder resource"):
        fetcher.fetch(f"https://{RESULT_HOST}/image", policy=_policy())


def test_pillow_verify_only_container_that_cannot_be_fully_decoded_is_rejected() -> None:
    valid = _image_bytes()
    idat_offset = valid.index(b"IDAT") - 4
    idat_length = int.from_bytes(valid[idat_offset : idat_offset + 4], "big")
    payload_start = idat_offset + 8
    compressed = valid[payload_start : payload_start + idat_length]
    truncated_payload = compressed[: max(1, len(compressed) // 2)]
    chunk_body = b"IDAT" + truncated_payload
    idat = (
        len(truncated_payload).to_bytes(4, "big")
        + chunk_body
        + (zlib.crc32(chunk_body) & 0xFFFFFFFF).to_bytes(4, "big")
    )
    truncated = valid[:idat_offset] + idat + valid[payload_start + idat_length + 4 :]
    with Image.open(io.BytesIO(truncated)) as image:
        image.verify()
    with Image.open(io.BytesIO(truncated)) as image:
        with pytest.raises(OSError):
            image.load()
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=truncated)
    )

    with pytest.raises(SafeArtifactError, match="fully decoded"):
        fetcher.fetch(f"https://{RESULT_HOST}/image", policy=_policy())


def test_animated_png_and_wildcard_policy_are_rejected() -> None:
    frames = [Image.new("RGB", (32, 24), color) for color in ((1, 2, 3), (4, 5, 6))]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    animated = buffer.getvalue()
    fetcher = _fetcher(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=animated)
    )
    with pytest.raises(SafeArtifactError, match="animated"):
        fetcher.fetch(f"https://{RESULT_HOST}/image", policy=_policy())

    with pytest.raises(SafeArtifactError, match="policy is invalid"):
        fetcher.fetch(
            f"https://{RESULT_HOST}/image",
            policy=_policy(hosts=frozenset({"*.example.test"})),
        )


def test_deadline_and_close_behavior_cover_failure_paths() -> None:
    closed = False

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"too much"

        def close(self) -> None:
            nonlocal closed
            closed = True

    now = 100.0
    fetcher = SafeArtifactFetcher(
        resolver=lambda _: [PUBLIC_IP],
        transport_factory=lambda *_: httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "image/png"}, stream=Stream())
        ),
        monotonic=lambda: now,
    )
    with pytest.raises(SafeArtifactError, match="byte limit"):
        fetcher.fetch(
            f"https://{RESULT_HOST}/image", policy=_policy(max_bytes=2), timeout_seconds=10
        )
    assert closed is True

    expired = SafeArtifactFetcher(resolver=lambda _: pytest.fail("expired deadline reached DNS"))
    with pytest.raises(SafeArtifactError) as error:
        expired.fetch(
            f"https://{RESULT_HOST}/image",
            policy=_policy(),
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    assert error.value.code == "RESULT_FETCH_DEADLINE"


def test_ownership_loss_stops_streaming_and_closes_artifact_response() -> None:
    closed = False
    yielded_chunks = 0
    checks = 0
    image_bytes = _image_bytes()

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal yielded_chunks
            for chunk in (image_bytes[:8], image_bytes[8:]):
                yielded_chunks += 1
                yield chunk

        def close(self) -> None:
            nonlocal closed
            closed = True

    def require_ownership() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise OwnershipLostError("test ownership lost")

    fetcher = SafeArtifactFetcher(
        resolver=lambda _: [PUBLIC_IP],
        transport_factory=lambda *_: httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=Stream(),
            )
        ),
    )

    with pytest.raises(OwnershipLostError):
        fetcher.fetch(
            f"https://{RESULT_HOST}/image",
            policy=_policy(),
            ownership_check=require_ownership,
        )

    assert checks == 4
    assert yielded_chunks == 1
    assert closed is True
