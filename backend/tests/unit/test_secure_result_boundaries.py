from __future__ import annotations

import hashlib

import httpx
import pytest

from museflow.assets import SecureResultDownloader


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_exact_official_host_download_has_expected_dimensions_size_and_digest() -> None:
    content = _png(1280, 1280)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=content)
        )
    ) as client:
        result = SecureResultDownloader(
            client=client,
            resolve_host=lambda _: ["93.184.216.34"],
            expected_dimensions=(1280, 1280),
        ).download("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/image.png")
    assert (result.width, result.height, result.size_bytes) == (1280, 1280, len(content))
    assert result.sha256 == hashlib.sha256(content).hexdigest()


def test_wrong_image_dimensions_and_dns_failure_stay_closed() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "image/png"}, content=_png(1024, 1024)
            )
        )
    ) as client:
        downloader = SecureResultDownloader(
            client=client,
            resolve_host=lambda _: ["93.184.216.34"],
            expected_dimensions=(1280, 1280),
        )
        with pytest.raises(ValueError, match="dimensions"):
            downloader.download("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/image")

        def failing_resolver(_: str) -> list[str]:
            raise OSError("fixture DNS failure")

        failing_downloader = SecureResultDownloader(client=client, resolve_host=failing_resolver)
        with pytest.raises(ValueError, match="resolved safely"):
            failing_downloader.download(
                "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/image"
            )
