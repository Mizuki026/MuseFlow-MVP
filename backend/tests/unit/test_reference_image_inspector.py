from __future__ import annotations

import io
import warnings
from pathlib import Path

import pytest
from PIL import Image

import museflow.reference_assets.image_inspector as inspector_module
from museflow.reference_assets.image_inspector import (
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_PIXELS,
    ImageInspectionError,
    ImageInspector,
)


def _image_bytes(
    *,
    fmt: str = "PNG",
    size: tuple[int, int] = (512, 512),
    mode: str = "RGB",
    animated: bool = False,
) -> bytes:
    alpha_mode = mode in {"RGBA", "LA", "PA", "RGBa", "La"}
    color: object = (
        128 if mode == "L" else (255, 0, 0, 128) if alpha_mode
        else (255, 0, 0, 0) if mode == "CMYK" else (255, 0, 0)
    )
    frames = [Image.new(mode, size, color=color)]
    if animated:
        next_color: object = (
            64 if mode == "L" else (0, 0, 255, 128) if alpha_mode
            else (0, 0, 255, 0) if mode == "CMYK" else (0, 0, 255)
        )
        frames.append(Image.new(mode, size, color=next_color))
    output = io.BytesIO()
    frames[0].save(
        output,
        format=fmt,
        save_all=animated,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def _inspect(tmp_path: Path, content: bytes, media_type: str = "image/png"):
    path = tmp_path / "upload.bin"
    path.write_bytes(content)
    return ImageInspector().inspect(path, declared_content_type=media_type)


@pytest.mark.parametrize(
    ("fmt", "media_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_accepts_fully_decoded_single_frame_rgb_images(
    tmp_path: Path, fmt: str, media_type: str
) -> None:
    content = _image_bytes(fmt=fmt)
    metadata = _inspect(tmp_path, content, media_type)

    assert metadata.content_type == media_type
    assert metadata.size_bytes == len(content)
    assert metadata.width == metadata.height == 512
    assert metadata.frame_count == 1
    assert metadata.color_mode == "RGB"
    assert len(metadata.sha256) == 64


def test_content_type_is_checked_against_decoded_format(tmp_path: Path) -> None:
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, _image_bytes(fmt="JPEG"), "image/png")

    assert error.value.code == "IMAGE_CONTENT_TYPE_MISMATCH"


@pytest.mark.parametrize(
    ("content", "media_type", "code"),
    [
        (b"not an image", "image/png", "IMAGE_FORMAT_UNSUPPORTED"),
        (_image_bytes()[:-20], "image/png", "IMAGE_CORRUPT"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png", "IMAGE_CORRUPT"),
    ],
)
def test_rejects_non_images_and_truncated_or_header_only_data(
    tmp_path: Path, content: bytes, media_type: str, code: str
) -> None:
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, content, media_type)

    assert error.value.code == code


@pytest.mark.parametrize(
    ("fmt", "mode", "expected_code"),
    [
        ("PNG", "RGBA", "IMAGE_ALPHA_NOT_ALLOWED"),
        ("WEBP", "RGBA", "IMAGE_ALPHA_NOT_ALLOWED"),
        ("PNG", "L", "IMAGE_COLOR_MODE_UNSUPPORTED"),
        ("JPEG", "CMYK", "IMAGE_COLOR_MODE_UNSUPPORTED"),
    ],
)
def test_rejects_alpha_and_non_rgb_modes(
    tmp_path: Path, fmt: str, mode: str, expected_code: str
) -> None:
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, _image_bytes(fmt=fmt, mode=mode), f"image/{fmt.lower()}")

    assert error.value.code == expected_code


def test_rejects_animated_webp(tmp_path: Path) -> None:
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, _image_bytes(fmt="WEBP", animated=True), "image/webp")

    assert error.value.code == "IMAGE_ANIMATED_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("size", "code"),
    [
        ((239, 512), "IMAGE_DIMENSIONS_UNSUPPORTED"),
        ((2049, 512), "IMAGE_DIMENSIONS_UNSUPPORTED"),
        ((480, 2048), "IMAGE_ASPECT_RATIO_UNSUPPORTED"),
    ],
)
def test_rejects_unsupported_dimensions(tmp_path: Path, size: tuple[int, int], code: str) -> None:
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, _image_bytes(size=size))

    assert error.value.code == code


def test_pillow_decompression_bomb_warning_is_a_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(ImageInspectionError) as error:
            _inspect(tmp_path, _image_bytes(size=(240, 240)))

    assert error.value.code == "IMAGE_DECOMPRESSION_BOMB"


def test_rejects_byte_and_pixel_limits_and_accepts_max_pixel_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _image_bytes()
    oversized = content + b"0" * (MAX_REFERENCE_BYTES + 1 - len(content))
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, oversized)
    assert error.value.code == "FILE_TOO_LARGE"

    assert MAX_REFERENCE_PIXELS == 4_194_304
    monkeypatch.setattr(inspector_module, "MAX_REFERENCE_PIXELS", 200_000)
    with pytest.raises(ImageInspectionError) as error:
        _inspect(tmp_path, content)
    assert error.value.code == "IMAGE_PIXEL_LIMIT_EXCEEDED"

    monkeypatch.setattr(inspector_module, "MAX_REFERENCE_PIXELS", MAX_REFERENCE_PIXELS)
    boundary = _image_bytes(size=(2048, 2048))
    metadata = _inspect(tmp_path, boundary)
    assert metadata.width * metadata.height == MAX_REFERENCE_PIXELS

    byte_boundary = content + b"0" * (MAX_REFERENCE_BYTES - len(content))
    metadata = _inspect(tmp_path, byte_boundary)
    assert metadata.size_bytes == MAX_REFERENCE_BYTES


def test_filename_extension_is_not_used_to_detect_image_format(tmp_path: Path) -> None:
    path = tmp_path / "actually-jpeg.png"
    content = _image_bytes(fmt="JPEG")
    path.write_bytes(content)

    metadata = ImageInspector().inspect(path, declared_content_type="image/jpeg")

    assert metadata.content_type == "image/jpeg"
