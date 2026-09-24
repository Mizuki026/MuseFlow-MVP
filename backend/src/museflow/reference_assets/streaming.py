from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from museflow.reference_assets.image_inspector import MAX_REFERENCE_BYTES

CHUNK_SIZE = 64 * 1024


class UploadTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TemporaryUpload:
    path: Path
    size_bytes: int
    sha256: str


async def stage_upload(
    read_chunk: Callable[[int], Awaitable[bytes]], *, max_bytes: int = MAX_REFERENCE_BYTES
) -> TemporaryUpload:
    descriptor, raw_path = tempfile.mkstemp(prefix="museflow-reference-")
    path = Path(raw_path)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as destination:
            while chunk := await read_chunk(CHUNK_SIZE):
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise UploadTooLargeError("image exceeds the maximum file size")
                digest.update(chunk)
                destination.write(chunk)
        return TemporaryUpload(path, size_bytes, digest.hexdigest())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
