from __future__ import annotations

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, ReferenceAssetModel
from museflow.db.session import create_session_factory
from museflow.reference_assets.__main__ import main as run_reference_asset_cli
from museflow.reference_assets.blob_store import (
    BlobMetadata,
    BlobNotFoundError,
    BlobStoreUnavailable,
)
from museflow.reference_assets.dispatch import (
    ReferenceMaintenanceOutboxDispatcher,
    ReferenceMaintenanceScheduler,
)
from museflow.reference_assets.image_inspector import ImageInspector
from museflow.reference_assets.maintenance import ReferenceAssetMaintenance
from museflow.reference_assets.object_keys import reference_object_key
from museflow.reference_assets.repository import (
    ReferenceAssetRepository,
    ReferenceAssetStateError,
)
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.domain import ReferenceAssetStatus


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.fail_stat = False
        self.fail_get = False
        self.fail_delete = False
        self.delete_calls: list[str] = []
        self.delete_started: threading.Event | None = None
        self.allow_delete: threading.Event | None = None
        self.lock = threading.Lock()

    def put(self, object_key, stream, *, size_bytes, content_type, sha256) -> None:
        content = stream.read()
        self.objects[object_key] = (content, content_type, sha256)

    def get(self, object_key: str, *, max_bytes: int = 6_000_000) -> bytes:
        if self.fail_get:
            raise BlobStoreUnavailable("injected read failure")
        try:
            content = self.objects[object_key][0]
            if len(content) > max_bytes:
                raise ValueError("too large")
            return content
        except KeyError as error:
            raise BlobNotFoundError("object is missing") from error

    def stat(self, object_key: str) -> BlobMetadata:
        if self.fail_stat:
            raise BlobStoreUnavailable("injected stat failure")
        try:
            content, content_type, sha256 = self.objects[object_key]
        except KeyError as error:
            raise BlobNotFoundError("object is missing") from error
        return BlobMetadata(len(content), content_type, sha256)

    def delete(self, object_key: str) -> None:
        with self.lock:
            self.delete_calls.append(object_key)
        if self.delete_started is not None:
            self.delete_started.set()
        if self.allow_delete is not None:
            assert self.allow_delete.wait(timeout=5)
        if self.fail_delete:
            raise BlobStoreUnavailable("injected delete failure")
        self.objects.pop(object_key, None)

    def presigned_get(self, object_key: str, *, expires_seconds: int = 300) -> str:
        return f"https://private.invalid/{object_key}?expires={expires_seconds}"


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 512), (44, 83, 120)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture()
def store_and_factory() -> tuple[sessionmaker[Session], InMemoryBlobStore]:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for reference lifecycle tests")
    return create_session_factory(database_url), InMemoryBlobStore()


def _create_asset(
    factory: sessionmaker[Session],
    blob_store: InMemoryBlobStore,
    *,
    status: ReferenceAssetStatus,
    created_at: datetime | None = None,
    store_object: bool = True,
) -> UUID:
    content = _png()
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(prefix="museflow-asset-test-", delete=False) as file:
        path = Path(file.name)
        file.write(content)
    try:
        metadata = ImageInspector().inspect(path, declared_content_type="image/png")
    finally:
        path.unlink(missing_ok=True)
    asset_id = uuid4()
    key = reference_object_key(asset_id, metadata.sha256, metadata.content_type)
    now = created_at or datetime.now(UTC)
    with factory.begin() as session:
        session.add(
            ReferenceAssetModel(
                id=asset_id,
                idempotency_key=f"lifecycle-{asset_id}",
                request_fingerprint=hashlib.sha256(str(asset_id).encode()).hexdigest(),
                status=status.value,
                object_key=key,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
                width=metadata.width,
                height=metadata.height,
                sha256=metadata.sha256,
                created_at=now,
                ready_at=now if status is ReferenceAssetStatus.READY else None,
                delete_pending_at=now if status is ReferenceAssetStatus.DELETE_PENDING else None,
            )
        )
    if store_object:
        blob_store.objects[key] = (content, metadata.content_type, metadata.sha256)
    return asset_id


def _cleanup(
    factory: sessionmaker[Session], blob_store: InMemoryBlobStore, asset_ids: list[UUID]
) -> None:
    with factory() as session:
        keys = list(
            session.scalars(
                select(ReferenceAssetModel.object_key).where(ReferenceAssetModel.id.in_(asset_ids))
            )
        )
    for key in keys:
        blob_store.objects.pop(key, None)
    with factory.begin() as session:
        session.execute(
            delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id.in_(asset_ids))
        )
        session.execute(
            delete(GenerationTaskModel).where(GenerationTaskModel.reference_asset_id.in_(asset_ids))
        )
        session.execute(delete(ReferenceAssetModel).where(ReferenceAssetModel.id.in_(asset_ids)))


def _insert_reference_task(session: Session, asset_id: UUID, sha256: str) -> UUID:
    now = datetime.now(UTC)
    task_id = uuid4()
    session.add(
        GenerationTaskModel(
            id=task_id,
            idempotency_key=f"reference-lock-task-{task_id}",
            request_fingerprint="a" * 64,
            prompt="internal reference lock test",
            size_preset="1280*1280",
            status="QUEUED",
            max_attempts=3,
            policy_version="reference-lock-test",
            deadline_at=now + timedelta(minutes=10),
            created_at=now,
            queued_at=now,
            version=1,
            generation_type="IMAGE_TO_IMAGE",
            reference_asset_id=asset_id,
            reference_sha256=sha256,
            provider_profile="mock-image-to-image-test",
            provider_name="mock",
            model_name="mock-reference-lock-test",
            capability_version="test-v1",
            policy_snapshot={"version": "test-v1"},
        )
    )
    session.flush()
    return task_id


def test_staging_recovery_readies_valid_object_and_fails_missing_or_invalid_objects(
    store_and_factory,
) -> None:
    factory, blob_store = store_and_factory
    now = datetime.now(UTC)
    ready_id = _create_asset(
        factory,
        blob_store,
        status=ReferenceAssetStatus.STAGING,
        created_at=now - timedelta(hours=2),
    )
    missing_id = _create_asset(
        factory,
        blob_store,
        status=ReferenceAssetStatus.STAGING,
        created_at=now - timedelta(hours=2),
        store_object=False,
    )
    invalid_id = _create_asset(
        factory,
        blob_store,
        status=ReferenceAssetStatus.STAGING,
        created_at=now - timedelta(hours=2),
    )
    with factory() as session:
        invalid = session.get(ReferenceAssetModel, invalid_id)
        assert invalid is not None
        blob_store.objects[invalid.object_key] = (b"bad object", "image/png", "0" * 64)
    try:
        maintenance = ReferenceAssetMaintenance(factory, blob_store, clock=lambda: now)
        summary = maintenance.run_batch()
        assert summary.staging_ready == 1
        assert summary.staging_failed == 2
        with factory() as session:
            statuses = {
                asset.id: (asset.status, asset.error_code)
                for asset in session.scalars(
                    select(ReferenceAssetModel).where(
                        ReferenceAssetModel.id.in_([ready_id, missing_id, invalid_id])
                    )
                )
            }
        assert statuses[ready_id] == ("READY", None)
        assert statuses[missing_id] == ("FAILED", "STAGED_OBJECT_MISSING")
        assert statuses[invalid_id] == ("FAILED", "STAGED_OBJECT_INVALID")
        assert invalid_id not in [UUID(key.split("/")[1]) for key in blob_store.objects]
    finally:
        _cleanup(factory, blob_store, [ready_id, missing_id, invalid_id])


def test_minio_failure_keeps_staging_and_dry_run_does_not_mutate_ready_asset(
    store_and_factory,
) -> None:
    factory, blob_store = store_and_factory
    now = datetime.now(UTC)
    staging_id = _create_asset(
        factory,
        blob_store,
        status=ReferenceAssetStatus.STAGING,
        created_at=now - timedelta(hours=2),
    )
    ready_id = _create_asset(
        factory, blob_store, status=ReferenceAssetStatus.READY, created_at=now - timedelta(hours=30)
    )
    repo = ReferenceAssetRepository()
    blob_store.fail_stat = True
    try:
        summary = ReferenceAssetMaintenance(factory, blob_store, clock=lambda: now).run_batch()
        assert summary.storage_retries == 1
        with factory() as session:
            staging = session.get(ReferenceAssetModel, staging_id)
            candidates = repo.report_unreferenced_ready(
                session, created_before=now - timedelta(hours=24), limit=10
            )
        assert staging is not None
        assert staging.status == "STAGING"
        assert staging.error_code == "OBJECT_STORE_UNAVAILABLE"
        assert [record.id for record in candidates] == [ready_id]
        with factory() as session:
            assert session.get(ReferenceAssetModel, ready_id).status == "READY"
        assert ready_id in [UUID(key.split("/")[1]) for key in blob_store.objects]
    finally:
        blob_store.fail_stat = False
        _cleanup(factory, blob_store, [staging_id, ready_id])


def test_explicit_deletion_claims_old_unreferenced_asset_then_worker_marks_deleted(
    store_and_factory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, blob_store = store_and_factory
    now = datetime.now(UTC)
    asset_id = _create_asset(
        factory, blob_store, status=ReferenceAssetStatus.READY, created_at=now - timedelta(hours=30)
    )
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    assert database_url is not None
    monkeypatch.setenv("MUSEFLOW_DATABASE_URL", database_url)
    try:
        assert run_reference_asset_cli(["report", "--minimum-age-hours", "24"]) == 0
        report = capsys.readouterr().out
        assert '"candidate_count": 1' in report
        assert str(asset_id) in report
        with factory() as session:
            assert session.get(ReferenceAssetModel, asset_id).status == "READY"

        assert run_reference_asset_cli(["delete-unreferenced", "--minimum-age-hours", "24"]) == 0
        assert '"dry_run": true' in capsys.readouterr().out
        with factory() as session:
            assert session.get(ReferenceAssetModel, asset_id).status == "READY"

        assert (
            run_reference_asset_cli(
                ["delete-unreferenced", "--minimum-age-hours", "24", "--execute", "--limit", "1"]
            )
            == 0
        )
        assert '"scheduled_count": 1' in capsys.readouterr().out
        with factory() as session:
            assert session.get(ReferenceAssetModel, asset_id).status == "DELETE_PENDING"
        assert ReferenceAssetMaintenance(factory, blob_store, clock=lambda: now).delete_asset(
            asset_id
        )
        with factory() as session:
            asset = session.get(ReferenceAssetModel, asset_id)
            assert asset is not None and asset.status == "DELETED"
        assert blob_store.objects == {}
    finally:
        _cleanup(factory, blob_store, [asset_id])


def test_delete_failure_remains_retryable_and_two_workers_cannot_claim_same_object(
    store_and_factory,
) -> None:
    factory, blob_store = store_and_factory
    now = datetime.now(UTC)
    failing_id = _create_asset(
        factory, blob_store, status=ReferenceAssetStatus.DELETE_PENDING, created_at=now
    )
    competing_id = _create_asset(
        factory, blob_store, status=ReferenceAssetStatus.DELETE_PENDING, created_at=now
    )
    try:
        blob_store.fail_delete = True
        assert not ReferenceAssetMaintenance(factory, blob_store, clock=lambda: now).delete_asset(
            failing_id
        )
        with factory() as session:
            pending = session.get(ReferenceAssetModel, failing_id)
            assert pending is not None and pending.status == "DELETE_PENDING"
            assert pending.error_code == "OBJECT_STORE_UNAVAILABLE"
        blob_store.fail_delete = False
        later = now + timedelta(seconds=31)
        assert ReferenceAssetMaintenance(factory, blob_store, clock=lambda: later).delete_asset(
            failing_id
        )

        blob_store.delete_started = threading.Event()
        blob_store.allow_delete = threading.Event()
        maintenance = ReferenceAssetMaintenance(factory, blob_store, clock=lambda: later)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(maintenance.delete_asset, competing_id)
            assert blob_store.delete_started.wait(timeout=5)
            second = pool.submit(maintenance.delete_asset, competing_id)
            assert second.result(timeout=5) is False
            blob_store.allow_delete.set()
            assert first.result(timeout=5) is True
        assert len([key for key in blob_store.delete_calls if f"/{competing_id}/" in key]) == 1
    finally:
        if blob_store.allow_delete is not None:
            blob_store.allow_delete.set()
        _cleanup(factory, blob_store, [failing_id, competing_id])


def test_reference_lock_and_delete_claim_are_serialized_and_fk_restricts_delete(
    store_and_factory,
) -> None:
    factory, blob_store = store_and_factory
    now = datetime.now(UTC)
    asset_id = _create_asset(
        factory, blob_store, status=ReferenceAssetStatus.READY, created_at=now - timedelta(days=2)
    )
    with factory() as session:
        asset = session.get(ReferenceAssetModel, asset_id)
        assert asset is not None
        digest = asset.sha256
    locked = threading.Event()
    release = threading.Event()
    deletion_result: list[list[object]] = []
    task_ids: list[UUID] = []
    repo = ReferenceAssetRepository()

    def create_reference() -> None:
        with factory.begin() as session:
            repo.lock_ready_for_reference(session, asset_id, expected_sha256=digest)
            locked.set()
            assert release.wait(timeout=5)
            task_ids.append(_insert_reference_task(session, asset_id, digest))

    def claim_delete() -> None:
        assert locked.wait(timeout=5)
        with factory.begin() as session:
            deletion_result.append(
                repo.claim_unreferenced_ready_for_deletion(
                    session,
                    now=now,
                    created_before=now - timedelta(hours=24),
                    limit=1,
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            creator = pool.submit(create_reference)
            deleter = pool.submit(claim_delete)
            deleter.result(timeout=5)
            release.set()
            creator.result(timeout=5)
        assert deletion_result == [[]]
        with factory() as session:
            assert session.get(ReferenceAssetModel, asset_id).status == "READY"
        with pytest.raises(IntegrityError):
            with factory.begin() as session:
                session.delete(session.get(ReferenceAssetModel, asset_id))

        # Once deletion commits first, the same lock seam rejects new task references.
        with factory.begin() as session:
            session.execute(
                delete(GenerationTaskModel).where(
                    GenerationTaskModel.reference_asset_id == asset_id
                )
            )
            repo.claim_unreferenced_ready_for_deletion(
                session,
                now=now,
                created_before=now - timedelta(hours=24),
                limit=1,
            )
        with pytest.raises(ReferenceAssetStateError):
            with factory.begin() as session:
                repo.lock_ready_for_reference(session, asset_id, expected_sha256=digest)
        with factory() as session:
            assert session.get(ReferenceAssetModel, asset_id).status == "DELETE_PENDING"
    finally:
        with factory.begin() as session:
            session.execute(
                delete(GenerationTaskModel).where(
                    GenerationTaskModel.reference_asset_id == asset_id
                )
            )
        _cleanup(factory, blob_store, [asset_id])


def test_maintenance_scheduler_and_outbox_dispatch_are_separate_from_core_outbox(
    store_and_factory,
) -> None:
    factory, _blob_store = store_and_factory
    now = datetime(2040, 1, 1, tzinfo=UTC)
    calls: list[tuple[str, UUID]] = []
    core_calls: list[UUID] = []
    core_id = uuid4()
    maintenance_id: UUID | None = None
    with factory.begin() as session:
        session.add(
            OutboxMessageModel(
                id=uuid4(),
                message_type="EXECUTE_TASK",
                aggregate_id=core_id,
                payload={"task_id": str(core_id)},
                available_at=now,
                created_at=now,
            )
        )

    class Publisher:
        def publish(self, message_type: str, asset_id: UUID) -> None:
            calls.append((message_type, asset_id))

    class CorePublisher:
        def publish(self, task_id: UUID) -> None:
            core_calls.append(task_id)

    scheduler = ReferenceMaintenanceScheduler(factory, clock=lambda: now)
    try:
        assert scheduler.run_once(interval_seconds=300)
        assert not scheduler.run_once(interval_seconds=300)
        with factory() as session:
            maintenance_id = session.scalar(
                select(OutboxMessageModel.id).where(
                    OutboxMessageModel.message_type == "RUN_REFERENCE_MAINTENANCE",
                    OutboxMessageModel.created_at == now,
                )
            )
        assert maintenance_id is not None
        assert OutboxDispatcher(factory, CorePublisher(), clock=lambda: now).dispatch_once() == 1
        assert core_calls == [core_id]

        class FailingMaintenancePublisher:
            def publish(self, message_type: str, asset_id: UUID) -> None:
                raise RuntimeError("maintenance broker unavailable")

        assert (
            ReferenceMaintenanceOutboxDispatcher(
                factory, FailingMaintenancePublisher(), clock=lambda: now
            ).dispatch_once()
            == 0
        )
        assert OutboxDispatcher(factory, CorePublisher(), clock=lambda: now).dispatch_once() == 0
        with factory() as session:
            message = session.get(OutboxMessageModel, maintenance_id)
            assert message is not None and message.published_at is None
        assert (
            ReferenceMaintenanceOutboxDispatcher(
                factory, Publisher(), clock=lambda: now
            ).dispatch_once()
            == 1
        )
        assert calls[0][0] == "RUN_REFERENCE_MAINTENANCE"
    finally:
        with factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.id.in_([maintenance_id]))
            )
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == core_id)
            )
