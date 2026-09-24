from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from museflow.reference_assets.blob_store import (
    BlobNotFoundError,
    BlobObjectTooLargeError,
    BlobStore,
    BlobStoreUnavailable,
)
from museflow.reference_assets.image_inspector import ImageInspectionError, ImageInspector
from museflow.reference_assets.repository import (
    ReferenceAssetRecord,
    ReferenceAssetRepository,
    ReferenceAssetStateError,
)
from museflow.tasks.domain import ReferenceAssetStatus


class ReferenceObjectContentMismatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedReferenceImage:
    asset_id: UUID
    content: bytes
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int


class ReferenceAssetReader:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        *,
        repository: ReferenceAssetRepository | None = None,
        inspector: ImageInspector | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store
        self._repository = repository or ReferenceAssetRepository()
        self._inspector = inspector or ImageInspector()

    def read(self, asset_id: UUID) -> VerifiedReferenceImage:
        with self._session_factory() as session:
            record = self._repository.find(session, asset_id)
        if record is None:
            raise LookupError("reference asset was not found")
        if record.status is not ReferenceAssetStatus.READY:
            raise ReferenceAssetStateError("reference asset is not ready")
        if (
            record.content_type is None
            or record.size_bytes is None
            or record.width is None
            or record.height is None
            or record.sha256 is None
        ):
            raise ReferenceObjectContentMismatchError("reference asset metadata is incomplete")
        try:
            stored = self._blob_store.stat(record.object_key)
        except BlobNotFoundError as error:
            raise FileNotFoundError("reference object is missing") from error
        if (
            stored.size_bytes != record.size_bytes
            or stored.content_type != record.content_type
            or (stored.sha256 is not None and stored.sha256 != record.sha256)
        ):
            raise ReferenceObjectContentMismatchError(
                "reference object does not match its metadata"
            )
        try:
            content = self._blob_store.get(record.object_key, max_bytes=record.size_bytes)
        except BlobObjectTooLargeError as error:
            raise ReferenceObjectContentMismatchError(
                "reference object exceeds its recorded size"
            ) from error
        if (
            len(content) != record.size_bytes
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            raise ReferenceObjectContentMismatchError(
                "reference object does not match its metadata"
            )
        with tempfile.NamedTemporaryFile(prefix="museflow-reference-read-", delete=False) as file:
            path = Path(file.name)
            file.write(content)
        try:
            actual = self._inspector.inspect(path, declared_content_type=record.content_type)
        except ImageInspectionError as error:
            raise ReferenceObjectContentMismatchError(
                "reference object image metadata is invalid"
            ) from error
        finally:
            path.unlink(missing_ok=True)
        if (
            actual.sha256 != record.sha256
            or actual.size_bytes != record.size_bytes
            or actual.content_type != record.content_type
            or actual.width != record.width
            or actual.height != record.height
        ):
            raise ReferenceObjectContentMismatchError(
                "reference object does not match its metadata"
            )
        return VerifiedReferenceImage(
            asset_id=asset_id,
            content=content,
            content_type=actual.content_type,
            size_bytes=actual.size_bytes,
            sha256=actual.sha256,
            width=actual.width,
            height=actual.height,
        )

    def record(self, asset_id: UUID) -> ReferenceAssetRecord:
        with self._session_factory() as session:
            record = self._repository.find(session, asset_id)
        if record is None:
            raise LookupError("reference asset was not found")
        return record


class AssetAccessSigner:
    """Create short-lived access URLs only from READY database records."""

    def __init__(self, reader: ReferenceAssetReader, blob_store: BlobStore) -> None:
        self._reader = reader
        self._blob_store = blob_store

    def sign(self, asset_id: UUID, *, expires_seconds: int = 300) -> str:
        if not 1 <= expires_seconds <= 300:
            raise ValueError("reference access URL lifetime must be at most 300 seconds")
        # Read and verify the current bytes before creating a short-lived object URL.
        self._reader.read(asset_id)
        record = self._reader.record(asset_id)
        if record.status is not ReferenceAssetStatus.READY:
            raise ReferenceAssetStateError("reference asset is not ready")
        try:
            return self._blob_store.presigned_get(
                record.object_key, expires_seconds=expires_seconds
            )
        except BlobStoreUnavailable:
            raise
