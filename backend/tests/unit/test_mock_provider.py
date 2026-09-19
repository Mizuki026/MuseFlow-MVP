from __future__ import annotations

import struct
import zlib

from museflow.providers import DASHSCOPE_SIZE, GenerationRequest, MockProvider


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
