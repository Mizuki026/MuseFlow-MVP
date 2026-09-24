from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from museflow.reference_assets.blob_store import (
    BlobNotFoundError,
    BlobObjectTooLargeError,
    BlobStore,
    BlobStoreUnavailable,
)
from museflow.reference_assets.image_inspector import (
    ImageInspectionError,
    ImageInspector,
)
from museflow.reference_assets.object_keys import reference_object_key
from museflow.reference_assets.repository import (
    ReferenceAssetRecord,
    ReferenceAssetRepository,
)
from museflow.tasks.domain import ReferenceAssetStatus


class ReferenceAssetConflictError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReferenceAssetUnavailableError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReferenceObjectMismatchError(RuntimeError):
    pass


class ReferenceObjectMissingError(FileNotFoundError):
    pass


class ReferenceAssetNotFoundError(LookupError):
    pass


def reference_request_fingerprint(
    *,
    sha256: str,
    content_type: str,
    width: int,
    height: int,
    size_bytes: int,
    frame_count: int,
    color_mode: str,
) -> str:
    payload = {
        "color_mode": color_mode,
        "content_type": content_type,
        "frame_count": frame_count,
        "height": height,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "width": width,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ReferenceAssetService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        *,
        repository: ReferenceAssetRepository | None = None,
        inspector: ImageInspector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store
        self._repository = repository or ReferenceAssetRepository()
        self._inspector = inspector or ImageInspector()
        self._clock = clock or (lambda: datetime.now(UTC))

    def upload(
        self,
        *,
        idempotency_key: str,
        path: Path,
        declared_content_type: str,
        upload_size_bytes: int,
        upload_sha256: str,
    ) -> tuple[ReferenceAssetRecord, bool]:
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ReferenceAssetUnavailableError(
                "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key is invalid"
            )
        try:
            image = self._inspector.inspect(path, declared_content_type=declared_content_type)
        except ImageInspectionError as error:
            raise ReferenceAssetUnavailableError(error.code, str(error)) from error
        if image.size_bytes != upload_size_bytes or image.sha256 != upload_sha256:
            raise ReferenceAssetUnavailableError(
                "IMAGE_UPLOAD_CHANGED", "uploaded image changed during validation"
            )

        fingerprint = reference_request_fingerprint(
            sha256=image.sha256,
            content_type=image.content_type,
            width=image.width,
            height=image.height,
            size_bytes=image.size_bytes,
            frame_count=image.frame_count,
            color_mode=image.color_mode,
        )
        record = self._find_by_key(idempotency_key)
        is_new = record is None
        if record is None:
            asset_id = uuid4()
            upload_lease_token = uuid4()
            now = self._clock()
            key = reference_object_key(asset_id, image.sha256, image.content_type)
            try:
                with self._session_factory.begin() as session:
                    record = self._repository.add_staging(
                        session,
                        asset_id=asset_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        object_key=key,
                        content_type=image.content_type,
                        size_bytes=image.size_bytes,
                        width=image.width,
                        height=image.height,
                        sha256=image.sha256,
                        created_at=now,
                        operation_lease_token=upload_lease_token,
                        operation_lease_expires_at=now + timedelta(minutes=5),
                    )
            except IntegrityError:
                record = self._find_by_key(idempotency_key)
                if record is None:
                    raise
                is_new = False

        if record.request_fingerprint != fingerprint:
            raise ReferenceAssetConflictError(
                "IDEMPOTENCY_KEY_CONFLICT", "Idempotency-Key was used for different image data"
            )
        if record.status is ReferenceAssetStatus.READY:
            return record, True
        if record.status is not ReferenceAssetStatus.STAGING:
            raise ReferenceAssetConflictError(
                "REFERENCE_ASSET_NOT_REUSABLE",
                "reference asset cannot be reused in its current state",
            )

        if is_new:
            lease_token = record.operation_lease_token
        else:
            now = self._clock()
            with self._session_factory.begin() as session:
                claim = self._repository.claim_asset_maintenance(
                    session,
                    record.id,
                    expected_status=ReferenceAssetStatus.STAGING,
                    now=now,
                    lease_seconds=300,
                )
            if claim is None:
                raise ReferenceAssetUnavailableError(
                    "REFERENCE_ASSET_PROCESSING", "reference asset is still being processed"
                )
            record, lease_token = claim
        if lease_token is None:
            raise ReferenceAssetUnavailableError(
                "REFERENCE_ASSET_PROCESSING", "reference asset is still being processed"
            )

        try:
            with path.open("rb") as source:
                self._blob_store.put(
                    record.object_key,
                    source,
                    size_bytes=image.size_bytes,
                    content_type=image.content_type,
                    sha256=image.sha256,
                )
            self.verify_object(record)
        except BlobStoreUnavailable as error:
            self._release_upload_lease(record.id, lease_token)
            raise ReferenceAssetUnavailableError(
                "OBJECT_STORE_UNAVAILABLE", "reference object storage is temporarily unavailable"
            ) from error
        except (BlobNotFoundError, ReferenceObjectMismatchError, ImageInspectionError) as error:
            self._release_upload_lease(
                record.id,
                lease_token,
                error_code="REFERENCE_OBJECT_INVALID",
                message="uploaded reference object failed verification",
            )
            raise ReferenceAssetUnavailableError(
                "REFERENCE_OBJECT_MISMATCH", "reference object could not be verified"
            ) from error

        try:
            with self._session_factory.begin() as session:
                marked_ready = self._repository.mark_ready(
                    session, record.id, now=self._clock(), lease_token=lease_token
                )
        except Exception as error:
            raise ReferenceAssetUnavailableError(
                "REFERENCE_ASSET_STATE_UPDATE_FAILED",
                "reference asset state could not be updated",
            ) from error
        current = self.get(record.id)
        if current is None:
            raise ReferenceAssetUnavailableError(
                "REFERENCE_ASSET_STATE_UPDATE_FAILED", "reference asset is unavailable"
            )
        if current.status is not ReferenceAssetStatus.READY:
            raise ReferenceAssetUnavailableError(
                "REFERENCE_ASSET_PROCESSING", "reference asset is still being processed"
            )
        return current, not is_new or not marked_ready

    def get(self, asset_id: UUID) -> ReferenceAssetRecord | None:
        with self._session_factory() as session:
            return self._repository.find(session, asset_id)

    def lock_ready_for_reference(
        self, session: Session, asset_id: UUID, *, expected_sha256: str
    ) -> ReferenceAssetRecord:
        """Lock a READY row for the full transaction that creates a task reference."""
        return self._repository.lock_ready_for_reference(
            session, asset_id, expected_sha256=expected_sha256
        )

    def _find_by_key(self, key: str) -> ReferenceAssetRecord | None:
        with self._session_factory() as session:
            return self._repository.find_by_idempotency_key(session, key)

    def _release_upload_lease(
        self,
        asset_id: UUID,
        lease_token: UUID,
        *,
        error_code: str = "OBJECT_STORE_UNAVAILABLE",
        message: str = "reference object storage is temporarily unavailable",
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._repository.release_maintenance(
                session,
                asset_id,
                lease_token=lease_token,
                now=now,
                error_code=error_code,
                message=message,
                retry_after_seconds=0,
            )

    def verify_object(self, record: ReferenceAssetRecord) -> None:
        # Keep metadata and bytes verification behind the BlobStore port.
        stat = self._blob_store.stat(record.object_key)
        expected_sha = record.sha256
        if (
            stat.size_bytes != record.size_bytes
            or stat.content_type != record.content_type
            or (stat.sha256 is not None and stat.sha256 != expected_sha)
        ):
            raise ReferenceObjectMismatchError("stored reference object metadata does not match")
        try:
            content = self._blob_store.get(record.object_key, max_bytes=record.size_bytes or 0)
        except BlobObjectTooLargeError as error:
            raise ReferenceObjectMismatchError(
                "stored reference object exceeds the recorded size"
            ) from error
        if len(content) != record.size_bytes or hashlib.sha256(content).hexdigest() != expected_sha:
            raise ReferenceObjectMismatchError("stored reference object content does not match")
        with tempfile.NamedTemporaryFile(
            prefix="museflow-reference-verify-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
        try:
            actual = self._inspector.inspect(
                temporary_path, declared_content_type=record.content_type or ""
            )
        finally:
            temporary_path.unlink(missing_ok=True)
        if (
            actual.sha256 != record.sha256
            or actual.content_type != record.content_type
            or actual.size_bytes != record.size_bytes
            or actual.width != record.width
            or actual.height != record.height
        ):
            raise ReferenceObjectMismatchError("stored reference object content does not match")
