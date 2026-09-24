from __future__ import annotations

import hashlib
import io
import struct
import zlib

import pytest
from PIL import Image

from museflow.providers import (
    DASHSCOPE_SIZE,
    GenerationRequest,
    ImageToImageProviderInput,
    MockProvider,
    PermanentProviderError,
    ProviderGenerationRequest,
    TransientProviderError,
)
from museflow.tasks.domain import GenerationType, VerifiedReferenceImage


def _png_details(content: bytes) -> tuple[int, int, int, bytes]:
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    position = 8
    width = height = color_type = 0
    image_data = bytearray()
    while position < len(content):
        length = struct.unpack(">I", content[position : position + 4])[0]
        chunk_type = content[position + 4 : position + 8]
        chunk = content[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
            assert bit_depth == 8
        elif chunk_type == b"IDAT":
            image_data.extend(chunk)
        elif chunk_type == b"IEND":
            break
    return width, height, color_type, zlib.decompress(image_data)


def test_mock_provider_returns_a_visible_1280_square_png() -> None:
    result = MockProvider().generate(
        GenerationRequest(prompt="雨后清晨的山谷", size_preset=DASHSCOPE_SIZE),
        request_key="task:attempt:1",
        remote_request_id=None,
    )

    width, height, color_type, raw = _png_details(result.content)
    assert result.content_type == "image/png"
    assert (width, height) == (1280, 1280)
    assert color_type == 2
    row_stride = 1 + width * 3
    assert len(raw) == height * row_stride
    row_colors = {raw[offset + 1 : offset + 4] for offset in range(0, len(raw), row_stride)}
    assert len(row_colors) > 1


def _verified_reference(color: tuple[int, int, int]) -> VerifiedReferenceImage:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(buffer, format="PNG")
    content = buffer.getvalue()
    return VerifiedReferenceImage(
        content=content,
        content_type="image/png",
        width=48,
        height=32,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _image_request(reference: VerifiedReferenceImage) -> ProviderGenerationRequest:
    return ProviderGenerationRequest(
        input=ImageToImageProviderInput(
            prompt="make it a watercolor landscape",
            size_preset=DASHSCOPE_SIZE,
            reference_image=reference,
        )
    )


def test_image_to_image_mock_uses_verified_bytes_and_is_deterministic() -> None:
    provider = MockProvider()
    first_reference = _verified_reference((20, 80, 140))
    request = _image_request(first_reference)
    first = provider.generate(request, request_key="stable-key", remote_request_id=None)
    replay = provider.generate(request, request_key="stable-key", remote_request_id=None)
    other = provider.generate(
        _image_request(_verified_reference((220, 40, 30))),
        request_key="stable-key",
        remote_request_id=None,
    )

    assert first.content == replay.content
    assert first.content != other.content
    assert hashlib.sha256(first.content).hexdigest() == first.metadata["sha256"]
    assert [call.generation_type for call in provider.call_records] == [
        GenerationType.IMAGE_TO_IMAGE,
        GenerationType.IMAGE_TO_IMAGE,
        GenerationType.IMAGE_TO_IMAGE,
    ]
    assert provider.call_records[0].reference_sha256 == first_reference.sha256


def test_image_to_image_mock_rejects_reference_bytes_that_do_not_match_the_digest() -> None:
    reference = _verified_reference((20, 80, 140))
    incorrect = VerifiedReferenceImage(
        content=reference.content + b"tampered",
        content_type=reference.content_type,
        width=reference.width,
        height=reference.height,
        sha256=reference.sha256,
    )
    with pytest.raises(PermanentProviderError) as error:
        MockProvider().generate(
            _image_request(incorrect), request_key="invalid", remote_request_id=None
        )
    assert error.value.code == "INPUT_REFERENCE_INVALID"


def test_image_to_image_remote_request_recovery_does_not_create_again() -> None:
    provider = MockProvider()
    reference = _verified_reference((20, 80, 140))
    remote_ids: list[str] = []
    request = _image_request(reference)
    first = provider.generate(
        request,
        request_key="recoverable",
        remote_request_id=None,
        on_remote_request_id=remote_ids.append,
    )
    resumed = provider.generate(
        request,
        request_key="recoverable",
        remote_request_id=remote_ids[0],
    )

    assert first.content == resumed.content
    assert remote_ids == ["mock-recoverable"]
    assert provider.create_calls == 1
    assert provider.recovery_calls == 1


def test_rate_limit_scenario_fails_before_creating_a_remote_request() -> None:
    provider = MockProvider(scenario="rate_limited")
    remote_ids: list[str] = []

    with pytest.raises(TransientProviderError) as error:
        provider.generate(
            GenerationRequest(prompt="rate-limited request", size_preset=DASHSCOPE_SIZE),
            request_key="rate-limited:attempt:1",
            remote_request_id=None,
            on_remote_request_id=remote_ids.append,
        )

    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert provider.create_calls == 0
    assert remote_ids == []

def test_timeout_scenario_is_transient_after_mock_submission() -> None:
    provider = MockProvider(scenario="timeout")
    request = GenerationRequest(prompt="timeout recovery", size_preset="1280*1280")

    with pytest.raises(TransientProviderError) as raised:
        provider.generate(request, request_key="timeout:attempt:1", remote_request_id=None)

    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert raised.value.retryable is True
    assert provider.create_calls == 1
    assert provider.poll_calls == 0
