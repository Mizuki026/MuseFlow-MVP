from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, ReferenceAssetModel
from museflow.tasks.domain import ReferenceAssetStatus


@dataclass(frozen=True, slots=True)
class ReferenceAssetRecord:
    id: UUID
    idempotency_key: str
    request_fingerprint: str
    status: ReferenceAssetStatus
    object_key: str
    content_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    sha256: str | None
    created_at: datetime
    ready_at: datetime | None
    delete_pending_at: datetime | None
    deleted_at: datetime | None
    error_code: str | None
    error_message: str | None
    operation_lease_token: UUID | None
    operation_lease_expires_at: datetime | None


def _record(model: ReferenceAssetModel) -> ReferenceAssetRecord:
    return ReferenceAssetRecord(
        id=model.id,
        idempotency_key=model.idempotency_key,
        request_fingerprint=model.request_fingerprint,
        status=ReferenceAssetStatus(model.status),
        object_key=model.object_key,
        content_type=model.content_type,
        size_bytes=model.size_bytes,
        width=model.width,
        height=model.height,
        sha256=model.sha256,
        created_at=model.created_at,
        ready_at=model.ready_at,
        delete_pending_at=model.delete_pending_at,
        deleted_at=model.deleted_at,
        error_code=model.error_code,
        error_message=model.error_message,
        operation_lease_token=model.operation_lease_token,
        operation_lease_expires_at=model.operation_lease_expires_at,
    )


class ReferenceAssetInUseError(RuntimeError):
    pass


class ReferenceAssetStateError(RuntimeError):
    def __init__(self, message: str, *, code: str = "REFERENCE_ASSET_NOT_READY") -> None:
        super().__init__(message)
        self.code = code


class ReferenceAssetRepository:
    def find_by_idempotency_key(self, session: Session, key: str) -> ReferenceAssetRecord | None:
        model = session.scalar(
            select(ReferenceAssetModel).where(ReferenceAssetModel.idempotency_key == key)
        )
        return _record(model) if model else None

    def find(self, session: Session, asset_id: UUID) -> ReferenceAssetRecord | None:
        model = session.get(ReferenceAssetModel, asset_id)
        return _record(model) if model else None

    def add_staging(
        self,
        session: Session,
        *,
        asset_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        object_key: str,
        content_type: str,
        size_bytes: int,
        width: int,
        height: int,
        sha256: str,
        created_at: datetime,
        operation_lease_token: UUID | None = None,
        operation_lease_expires_at: datetime | None = None,
    ) -> ReferenceAssetRecord:
        model = ReferenceAssetModel(
            id=asset_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            status=ReferenceAssetStatus.STAGING.value,
            object_key=object_key,
            content_type=content_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            sha256=sha256,
            created_at=created_at,
            operation_lease_token=operation_lease_token,
            operation_lease_expires_at=operation_lease_expires_at,
        )
        session.add(model)
        session.flush()
        return _record(model)

    def mark_ready(
        self,
        session: Session,
        asset_id: UUID,
        *,
        now: datetime,
        lease_token: UUID | None = None,
    ) -> bool:
        conditions = [
            ReferenceAssetModel.id == asset_id,
            ReferenceAssetModel.status == ReferenceAssetStatus.STAGING.value,
        ]
        if lease_token is not None:
            conditions.append(ReferenceAssetModel.operation_lease_token == lease_token)
        result = session.execute(
            update(ReferenceAssetModel)
            .where(*conditions)
            .values(
                status=ReferenceAssetStatus.READY.value,
                ready_at=now,
                error_code=None,
                error_message=None,
                operation_lease_token=None,
                operation_lease_expires_at=None,
            )
        )
        return cast(Any, getattr(result, "rowcount", 0)) == 1

    def mark_failed(
        self,
        session: Session,
        asset_id: UUID,
        *,
        code: str,
        message: str,
        lease_token: UUID | None = None,
    ) -> bool:
        conditions = [
            ReferenceAssetModel.id == asset_id,
            ReferenceAssetModel.status == ReferenceAssetStatus.STAGING.value,
        ]
        if lease_token is not None:
            conditions.append(ReferenceAssetModel.operation_lease_token == lease_token)
        result = session.execute(
            update(ReferenceAssetModel)
            .where(*conditions)
            .values(
                status=ReferenceAssetStatus.FAILED.value,
                error_code=code,
                error_message=message,
                operation_lease_token=None,
                operation_lease_expires_at=None,
            )
        )
        return cast(Any, getattr(result, "rowcount", 0)) == 1

    def record_staging_error(
        self,
        session: Session,
        asset_id: UUID,
        *,
        code: str,
        message: str,
    ) -> None:
        session.execute(
            update(ReferenceAssetModel)
            .where(
                ReferenceAssetModel.id == asset_id,
                ReferenceAssetModel.status == ReferenceAssetStatus.STAGING.value,
            )
            .values(error_code=code, error_message=message)
        )

    def lock_ready_for_reference(
        self,
        session: Session,
        asset_id: UUID,
        *,
        expected_sha256: str | None = None,
    ) -> ReferenceAssetRecord:
        model = session.scalar(
            select(ReferenceAssetModel).where(ReferenceAssetModel.id == asset_id).with_for_update()
        )
        if model is None:
            raise ReferenceAssetStateError(
                "reference asset was not found", code="REFERENCE_ASSET_NOT_FOUND"
            )
        if model.status != ReferenceAssetStatus.READY.value:
            raise ReferenceAssetStateError("reference asset is not ready")
        if (
            model.sha256 is None
            or len(model.sha256) != 64
            or any(character not in "0123456789abcdef" for character in model.sha256)
        ):
            raise ReferenceAssetStateError(
                "reference asset digest is unavailable", code="REFERENCE_ASSET_INVALID"
            )
        if expected_sha256 is not None and model.sha256 != expected_sha256:
            raise ReferenceAssetStateError(
                "reference asset digest changed", code="REFERENCE_ASSET_INVALID"
            )
        return _record(model)

    def report_unreferenced_ready(
        self, session: Session, *, created_before: datetime, limit: int
    ) -> list[ReferenceAssetRecord]:
        referenced = exists(
            select(GenerationTaskModel.id).where(
                GenerationTaskModel.reference_asset_id == ReferenceAssetModel.id
            )
        )
        models = session.scalars(
            select(ReferenceAssetModel)
            .where(
                ReferenceAssetModel.status == ReferenceAssetStatus.READY.value,
                ReferenceAssetModel.created_at <= created_before,
                ~referenced,
            )
            .order_by(ReferenceAssetModel.created_at.asc(), ReferenceAssetModel.id.asc())
            .limit(limit)
        )
        return [_record(model) for model in models]

    def count_unreferenced_ready(self, session: Session, *, created_before: datetime) -> int:
        referenced = exists(
            select(GenerationTaskModel.id).where(
                GenerationTaskModel.reference_asset_id == ReferenceAssetModel.id
            )
        )
        return int(
            session.scalar(
                select(func.count())
                .select_from(ReferenceAssetModel)
                .where(
                    ReferenceAssetModel.status == ReferenceAssetStatus.READY.value,
                    ReferenceAssetModel.created_at <= created_before,
                    ~referenced,
                )
            )
            or 0
        )

    def claim_unreferenced_ready_for_deletion(
        self,
        session: Session,
        *,
        now: datetime,
        created_before: datetime,
        limit: int,
    ) -> list[ReferenceAssetRecord]:
        referenced = exists(
            select(GenerationTaskModel.id).where(
                GenerationTaskModel.reference_asset_id == ReferenceAssetModel.id
            )
        )
        assets = list(
            session.scalars(
                select(ReferenceAssetModel)
                .where(
                    ReferenceAssetModel.status == ReferenceAssetStatus.READY.value,
                    ReferenceAssetModel.created_at <= created_before,
                    ~referenced,
                )
                .order_by(ReferenceAssetModel.created_at.asc(), ReferenceAssetModel.id.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        result: list[ReferenceAssetRecord] = []
        for asset in assets:
            # The row lock is shared with task-reference creation; recheck after acquiring it.
            if (
                session.scalar(
                    select(GenerationTaskModel.id)
                    .where(GenerationTaskModel.reference_asset_id == asset.id)
                    .limit(1)
                )
                is not None
            ):
                continue
            asset.status = ReferenceAssetStatus.DELETE_PENDING.value
            asset.delete_pending_at = now
            asset.error_code = None
            asset.error_message = None
            asset.operation_lease_token = None
            asset.operation_lease_expires_at = None
            session.add(
                OutboxMessageModel(
                    id=uuid4(),
                    message_type="DELETE_REFERENCE_ASSET",
                    aggregate_id=asset.id,
                    payload={"asset_id": str(asset.id)},
                    available_at=now,
                    created_at=now,
                )
            )
            result.append(_record(asset))
        return result

    def claim_maintenance_batch(
        self,
        session: Session,
        *,
        now: datetime,
        staging_before: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[tuple[ReferenceAssetRecord, UUID]]:
        eligible_statuses = (
            ReferenceAssetStatus.STAGING.value,
            ReferenceAssetStatus.DELETE_PENDING.value,
        )
        assets = list(
            session.scalars(
                select(ReferenceAssetModel)
                .where(
                    ReferenceAssetModel.status.in_(eligible_statuses),
                    (
                        ReferenceAssetModel.operation_lease_expires_at.is_(None)
                        | (ReferenceAssetModel.operation_lease_expires_at <= now)
                    ),
                    (
                        (ReferenceAssetModel.status == ReferenceAssetStatus.DELETE_PENDING.value)
                        | (ReferenceAssetModel.created_at <= staging_before)
                    ),
                )
                .order_by(ReferenceAssetModel.created_at.asc(), ReferenceAssetModel.id.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        claimed: list[tuple[ReferenceAssetRecord, UUID]] = []
        for asset in assets:
            token = uuid4()
            asset.operation_lease_token = token
            asset.operation_lease_expires_at = now + timedelta(seconds=lease_seconds)
            claimed.append((_record(asset), token))
        return claimed

    def claim_asset_maintenance(
        self,
        session: Session,
        asset_id: UUID,
        *,
        expected_status: ReferenceAssetStatus,
        now: datetime,
        lease_seconds: int,
    ) -> tuple[ReferenceAssetRecord, UUID] | None:
        asset = session.scalar(
            select(ReferenceAssetModel)
            .where(
                ReferenceAssetModel.id == asset_id,
                ReferenceAssetModel.status == expected_status.value,
                (
                    ReferenceAssetModel.operation_lease_expires_at.is_(None)
                    | (ReferenceAssetModel.operation_lease_expires_at <= now)
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if asset is None:
            return None
        token = uuid4()
        asset.operation_lease_token = token
        asset.operation_lease_expires_at = now + timedelta(seconds=lease_seconds)
        return _record(asset), token

    def finish_delete(
        self,
        session: Session,
        asset_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
    ) -> bool:
        result = session.execute(
            update(ReferenceAssetModel)
            .where(
                ReferenceAssetModel.id == asset_id,
                ReferenceAssetModel.status == ReferenceAssetStatus.DELETE_PENDING.value,
                ReferenceAssetModel.operation_lease_token == lease_token,
            )
            .values(
                status=ReferenceAssetStatus.DELETED.value,
                deleted_at=now,
                error_code=None,
                error_message=None,
                operation_lease_token=None,
                operation_lease_expires_at=None,
            )
        )
        return cast(Any, getattr(result, "rowcount", 0)) == 1

    def release_maintenance(
        self,
        session: Session,
        asset_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
        error_code: str,
        message: str,
        retry_after_seconds: int = 30,
    ) -> bool:
        result = session.execute(
            update(ReferenceAssetModel)
            .where(
                ReferenceAssetModel.id == asset_id,
                ReferenceAssetModel.operation_lease_token == lease_token,
            )
            .values(
                error_code=error_code,
                error_message=message,
                operation_lease_expires_at=now + timedelta(seconds=retry_after_seconds),
            )
        )
        return cast(Any, getattr(result, "rowcount", 0)) == 1
