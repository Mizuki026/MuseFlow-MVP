from __future__ import annotations

from uuid import UUID

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def reference_object_key(asset_id: UUID, sha256: str, content_type: str) -> str:
    extension = _EXTENSIONS.get(content_type)
    if extension is None:
        raise ValueError("unsupported reference image content type")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be a lowercase hexadecimal digest")
    return f"references/{asset_id}/{sha256}.{extension}"
