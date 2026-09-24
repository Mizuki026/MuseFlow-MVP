from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from museflow.reference_assets.blob_store import BlobNotFoundError, BlobStore, BlobStoreUnavailable
from museflow.reference_assets.image_inspector import ImageInspectionError
from museflow.reference_assets.repository import ReferenceAssetRecord, ReferenceAssetRepository
from museflow.reference_assets.service import (
    ReferenceAssetService,
    ReferenceObjectMismatchError,
)
from museflow.safe_logging import log_task_event
from museflow.tasks.domain import ReferenceAssetStatus

logger = logging.getLogger(__name__)
STAGING_RECOVERY_AFTER = timedelta(hours=1)
MAINTENANCE_LEASE_SECONDS = 120
MAINTENANCE_RETRY_SECONDS = 30
MAINTENANCE_BATCH_SIZE = 25


@dataclass(frozen=True, slots=True)
class MaintenanceSummary:
    staging_ready: int = 0
    staging_failed: int = 0
    storage_retries: int = 0
    deleted: int = 0
    delete_retries: int = 0


class ReferenceAssetMaintenance:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        *,
        repository: ReferenceAssetRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = MAINTENANCE_LEASE_SECONDS,
        batch_size: int = MAINTENANCE_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store
        self._repository = repository or ReferenceAssetRepository()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._verifier = ReferenceAssetService(
            session_factory, blob_store, repository=self._repository, clock=self._clock
        )

    def run_batch(self) -> MaintenanceSummary:
        now = self._clock()
        with self._session_factory.begin() as session:
            claimed = self._repository.claim_maintenance_batch(
                session,
                now=now,
                staging_before=now - STAGING_RECOVERY_AFTER,
                lease_seconds=self._lease_seconds,
                limit=self._batch_size,
            )
        staging_ready = staging_failed = storage_retries = deleted = delete_retries = 0
        for record, lease_token in claimed:
            if record.status is ReferenceAssetStatus.STAGING:
                outcome = self._recover_staging(record, lease_token)
                if outcome == "ready":
                    staging_ready += 1
                elif outcome == "failed":
                    staging_failed += 1
                elif outcome == "retry":
                    storage_retries += 1
            elif record.status is ReferenceAssetStatus.DELETE_PENDING:
                if self._delete_pending(record, lease_token):
                    deleted += 1
                else:
                    delete_retries += 1
        return MaintenanceSummary(
            staging_ready=staging_ready,
            staging_failed=staging_failed,
            storage_retries=storage_retries,
            deleted=deleted,
            delete_retries=delete_retries,
        )

    def delete_asset(self, asset_id: UUID) -> bool:
        now = self._clock()
        with self._session_factory.begin() as session:
            claim = self._repository.claim_asset_maintenance(
                session,
                asset_id,
                expected_status=ReferenceAssetStatus.DELETE_PENDING,
                now=now,
                lease_seconds=self._lease_seconds,
            )
        if claim is None:
            return False
        record, token = claim
        return self._delete_pending(record, token)

    def _recover_staging(self, record: ReferenceAssetRecord, lease_token: UUID) -> str:
        started_at = time.monotonic()
        try:
            self._verifier.verify_object(record)
        except BlobNotFoundError:
            with self._session_factory.begin() as session:
                self._repository.mark_failed(
                    session,
                    record.id,
                    code="STAGED_OBJECT_MISSING",
                    message="staged object was not found during recovery",
                    lease_token=lease_token,
                )
            log_task_event(
                logger,
                "reference_asset_staging_recovery_failed",
                level=logging.WARNING,
                asset_id=str(record.id),
                error_code="STAGED_OBJECT_MISSING",
                recovery=True,
                maintenance_task=True,
                status="failed",
                duration_ms=(time.monotonic() - started_at) * 1000,
            )
            return "failed"
        except BlobStoreUnavailable:
            self._release_after_storage_failure(record.id, lease_token, "STAGING")
            log_task_event(
                logger,
                "reference_asset_staging_recovery_deferred",
                level=logging.WARNING,
                asset_id=str(record.id),
                error_code="OBJECT_STORE_UNAVAILABLE",
                recovery=True,
                maintenance_task=True,
                status="retry",
                duration_ms=(time.monotonic() - started_at) * 1000,
            )
            return "retry"
        except (ReferenceObjectMismatchError, ImageInspectionError):
            with self._session_factory.begin() as session:
                self._repository.mark_failed(
                    session,
                    record.id,
                    code="STAGED_OBJECT_INVALID",
                    message="staged object failed image integrity checks",
                    lease_token=lease_token,
                )
            try:
                self._blob_store.delete(record.object_key)
            except BlobStoreUnavailable:
                log_task_event(
                    logger,
                    "reference_asset_invalid_object_cleanup_deferred",
                    level=logging.WARNING,
                    asset_id=str(record.id),
                    error_code="OBJECT_STORE_UNAVAILABLE",
                    maintenance_task=True,
                    status="retry",
                )
            log_task_event(
                logger,
                "reference_asset_staging_recovery_failed",
                level=logging.WARNING,
                asset_id=str(record.id),
                error_code="STAGED_OBJECT_INVALID",
                recovery=True,
                maintenance_task=True,
                status="failed",
                duration_ms=(time.monotonic() - started_at) * 1000,
            )
            return "failed"
        with self._session_factory.begin() as session:
            ready = self._repository.mark_ready(
                session,
                record.id,
                now=self._clock(),
                lease_token=lease_token,
            )
        log_task_event(
            logger,
            "reference_asset_staging_recovered",
            asset_id=str(record.id),
            recovery=True,
            maintenance_task=True,
            status="ready" if ready else "unchanged",
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
        return "ready" if ready else "skipped"

    def _delete_pending(self, record: ReferenceAssetRecord, lease_token: UUID) -> bool:
        try:
            self._blob_store.delete(record.object_key)
        except BlobNotFoundError:
            pass
        except BlobStoreUnavailable:
            self._release_after_storage_failure(record.id, lease_token, "DELETE_PENDING")
            return False
        with self._session_factory.begin() as session:
            return self._repository.finish_delete(
                session,
                record.id,
                lease_token=lease_token,
                now=self._clock(),
            )

    def _release_after_storage_failure(
        self, asset_id: UUID, lease_token: UUID, status: str
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._repository.release_maintenance(
                session,
                asset_id,
                lease_token=lease_token,
                now=now,
                error_code="OBJECT_STORE_UNAVAILABLE",
                message=f"object storage operation failed while asset was {status}",
                retry_after_seconds=MAINTENANCE_RETRY_SECONDS,
            )
