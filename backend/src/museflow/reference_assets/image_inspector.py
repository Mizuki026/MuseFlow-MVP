from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError

MAX_REFERENCE_BYTES: Final = 6_000_000
MIN_REFERENCE_EDGE: Final = 240
MAX_REFERENCE_EDGE: Final = 2_048
MAX_REFERENCE_PIXELS: Final = 4_194_304
MIN_ASPECT_RATIO: Final = 0.25
MAX_ASPECT_RATIO: Final = 4.0
_FORMAT_CONTENT_TYPES: Final = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


class ImageInspectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InspectedImage:
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    frame_count: int
    color_mode: str


class ImageInspector:
    """Validate an uploaded image without depending on transport or storage concerns."""

    def inspect(
        self, path: Path | str, *, declared_content_type: str
    ) -> InspectedImage:
        image_path = Path(path)
        try:
            size_bytes = image_path.stat().st_size
        except OSError as error:
            raise ImageInspectionError("IMAGE_CORRUPT", "image file cannot be read") from error
        if size_bytes > MAX_REFERENCE_BYTES:
            raise ImageInspectionError("FILE_TOO_LARGE", "image exceeds the maximum file size")
        if size_bytes <= 0:
            raise ImageInspectionError("IMAGE_CORRUPT", "image file is empty")

        actual_format = self._detect_signature(image_path)
        actual_content_type = _FORMAT_CONTENT_TYPES.get(actual_format)
        if actual_content_type is None:
            raise ImageInspectionError("IMAGE_FORMAT_UNSUPPORTED", "image format is not supported")
        if declared_content_type != actual_content_type:
            raise ImageInspectionError(
                "IMAGE_CONTENT_TYPE_MISMATCH",
                "declared media type does not match the image content",
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(image_path) as image:
                    if image.format != actual_format:
                        raise ImageInspectionError(
                            "IMAGE_CORRUPT", "image signature does not match its decoded format"
                        )
                    width, height = image.size
                    frame_count = getattr(image, "n_frames", 1)
                    color_mode = image.mode
                    has_alpha = "A" in image.getbands() or "transparency" in image.info
                    self._validate_metadata(width, height, frame_count, color_mode, has_alpha)
                    image.verify()

                # verify() checks container structure, while this second open fully decodes pixels.
                with Image.open(image_path) as image:
                    image.load()
                    if image.size != (width, height) or image.mode != color_mode:
                        raise ImageInspectionError(
                            "IMAGE_CORRUPT", "decoded image metadata changed during validation"
                        )
        except ImageInspectionError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ImageInspectionError(
                "IMAGE_DECOMPRESSION_BOMB", "image exceeds the safe decoder resource limit"
            ) from error
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as error:
            raise ImageInspectionError("IMAGE_CORRUPT", "image is damaged or truncated") from error

        digest = hashlib.sha256()
        try:
            with image_path.open("rb") as image_file:
                for chunk in iter(lambda: image_file.read(64 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ImageInspectionError("IMAGE_CORRUPT", "image file cannot be read") from error

        return InspectedImage(
            content_type=actual_content_type,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
            width=width,
            height=height,
            frame_count=frame_count,
            color_mode=color_mode,
        )

    @staticmethod
    def _detect_signature(path: Path) -> str | None:
        try:
            with path.open("rb") as image_file:
                header = image_file.read(12)
        except OSError as error:
            raise ImageInspectionError("IMAGE_CORRUPT", "image file cannot be read") from error
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "PNG"
        if header.startswith(b"\xff\xd8\xff"):
            return "JPEG"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "WEBP"
        return None

    @staticmethod
    def _validate_metadata(
        width: int, height: int, frame_count: int, color_mode: str, has_alpha: bool
    ) -> None:
        if frame_count != 1:
            raise ImageInspectionError(
                "IMAGE_ANIMATED_NOT_ALLOWED", "animated images are not supported"
            )
        if has_alpha:
            raise ImageInspectionError(
                "IMAGE_ALPHA_NOT_ALLOWED", "alpha channels are not supported"
            )
        if color_mode != "RGB":
            raise ImageInspectionError(
                "IMAGE_COLOR_MODE_UNSUPPORTED", "image must use RGB color mode"
            )
        if (
            width < MIN_REFERENCE_EDGE
            or height < MIN_REFERENCE_EDGE
            or width > MAX_REFERENCE_EDGE
            or height > MAX_REFERENCE_EDGE
        ):
            raise ImageInspectionError(
                "IMAGE_DIMENSIONS_UNSUPPORTED", "image dimensions are outside the supported range"
            )
        if width * height > MAX_REFERENCE_PIXELS:
            raise ImageInspectionError(
                "IMAGE_PIXEL_LIMIT_EXCEEDED", "image has too many pixels"
            )
        aspect_ratio = width / height
        if not MIN_ASPECT_RATIO <= aspect_ratio <= MAX_ASPECT_RATIO:
            raise ImageInspectionError(
                "IMAGE_ASPECT_RATIO_UNSUPPORTED", "image aspect ratio is not supported"
            )
