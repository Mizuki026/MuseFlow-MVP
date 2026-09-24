from __future__ import annotations

import hashlib
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import delete, event, select

from museflow.api.app import create_app
from museflow.assets import (
    MinioResultAssetStore,
    candidate_object_key,
    result_identity,
)
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.providers import GenerationRequest, GenerationResult, MockProvider
from museflow.tasks.application import CreateTask
from museflow.tasks.domain import CreateTaskRequest, TaskStatus
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.recovery import RecoverExpiredLeases
from museflow.tasks.result_publication import PublicationDecision, ResultPublicationStatus


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + bytes(rgb))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


def _generation_result(content: bytes) -> GenerationResult:
    digest = hashlib.sha256(content).hexdigest()
    return GenerationResult(
        provider_name="test",
        provider_request_id=None,
        result_digest=digest,
        metadata={"sha256": digest},
        content=content,
        content_type="image/png",
    )


class StaticProvider:
    name = "static-test"

    def __init__(
        self,
        result: GenerationResult,
        *,
        started: Event | None = None,
        resume: Event | None = None,
    ) -> None:
        self._result = result
        self._started = started
        self._resume = resume

    def generate(
        self,
        request,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id=None,
    ) -> GenerationResult:
        del request, request_key, remote_request_id, on_remote_request_id
        if self._started is not None:
            self._started.set()
        if self._resume is not None and not self._resume.wait(timeout=10):
            raise TimeoutError("stale worker was not resumed")
        return self._result


class CapturingExecuteGenerationAttempt(ExecuteGenerationAttempt):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last_claim = None

    def _claim(self, task_id: UUID):
        self.last_claim = super()._claim(task_id)
        return self.last_claim


def _minio_client() -> Minio:
    endpoint = os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
    scheme, separator, host = endpoint.partition("://")
    if not separator:
        scheme, host = "http", endpoint
    return Minio(
        host,
        access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        secure=scheme == "https",
    )


def _read_object(client: Minio, bucket: str, object_key: str) -> bytes:
    response = client.get_object(bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def _cleanup_task(factory, task_id: UUID) -> None:
    with factory.begin() as session:
        session.execute(
            delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
        )
        task = session.get(GenerationTaskModel, task_id)
        if task is not None:
            session.delete(task)


def _require_minio_integration() -> None:
    if os.environ.get("MUSEFLOW_RUN_RESULT_PUBLICATION_INTEGRATION") != "1":
        pytest.skip("set MUSEFLOW_RUN_RESULT_PUBLICATION_INTEGRATION=1 with PostgreSQL and MinIO")


def test_new_worker_result_stays_authoritative_when_stale_worker_writes_later() -> None:
    _require_minio_integration()
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    factory = create_session_factory(database_url)
    store = MinioResultAssetStore()
    stale_store = MinioResultAssetStore()
    store.check_ready()
    minio = _minio_client()
    bucket = os.environ.get("MINIO_BUCKET", "museflow-results")
    clock = MutableClock()
    task_id = CreateTask(factory, clock=clock).execute(
        CreateTaskRequest(prompt="result fencing race"), f"result-race-{uuid4()}"
    ).task.id
    object_keys: set[str] = set()

    try:
        stale_started = Event()
        resume_stale = Event()
        winning_result = _generation_result(_png((20, 60, 200)))
        stale_result = _generation_result(_png((220, 40, 30)))
        stale_execute = ExecuteGenerationAttempt(
            factory,
            StaticProvider(stale_result, started=stale_started, resume=resume_stale),
            asset_store=stale_store,
            clock=clock,
            lease_seconds=10,
        )
        winning_execute = CapturingExecuteGenerationAttempt(
            factory,
            StaticProvider(winning_result),
            asset_store=store,
            clock=clock,
            lease_seconds=10,
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            stale_future = workers.submit(stale_execute.execute, task_id)
            assert stale_started.wait(timeout=5)
            stale_identity = result_identity(stale_result.content, stale_result.content_type)
            with factory() as session:
                claimed_attempt = session.scalar(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            assert claimed_attempt is not None
            stale_key = candidate_object_key(task_id, claimed_attempt.id, stale_identity)
            object_keys.add(stale_key)

            clock.now += timedelta(seconds=11)
            assert RecoverExpiredLeases(factory, clock=clock).run_once() == 1
            winning_outcome = winning_execute.execute(task_id)
            assert winning_outcome.succeeded is True
            assert winning_outcome.publication_status is ResultPublicationStatus.PUBLISHED

            with factory() as session:
                winning_asset = session.scalar(
                    select(ResultAssetModel).where(
                        ResultAssetModel.task_id == task_id,
                        ResultAssetModel.role == "RESULT",
                    )
                )
                assert winning_asset is not None

            with ThreadPoolExecutor(max_workers=2) as same_content_writes:
                repeated_writes = list(
                    same_content_writes.map(
                        lambda _: store.put_result(
                            task_id=task_id,
                            attempt_id=winning_asset.attempt_id,
                            content=winning_result.content,
                            content_type=winning_result.content_type,
                        ),
                        range(2),
                    )
                )
            assert repeated_writes[0] == repeated_writes[1]
            winning_candidate = repeated_writes[0]
            object_keys.add(winning_candidate.object_key)
            assert winning_candidate.sha256 == hashlib.sha256(winning_result.content).hexdigest()
            assert winning_execute.last_claim is not None
            assert winning_execute._succeed(
                winning_execute.last_claim, winning_result, winning_candidate
            ) is PublicationDecision.ALREADY_PUBLISHED

            resume_stale.set()
            stale_outcome = stale_future.result(timeout=10)
            assert stale_outcome.succeeded is False
            assert stale_outcome.publication_status is ResultPublicationStatus.OWNERSHIP_LOST
            assert stale_outcome.candidate_persisted is True

        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(
                        ResultAssetModel.task_id == task_id,
                        ResultAssetModel.role == "RESULT",
                    )
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert len(assets) == 1
        assert assets[0].attempt_id == winning_asset.attempt_id
        assert assets[0].object_key == winning_candidate.object_key
        assert assets[0].sha256 == winning_candidate.sha256
        assert _read_object(minio, bucket, assets[0].object_key) == winning_result.content
        assert stale_key != winning_candidate.object_key
        assert _read_object(minio, bucket, stale_key) == stale_result.content

        client = TestClient(create_app(session_factory=factory, asset_store=store))
        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["result"]["sha256"] == winning_candidate.sha256
        assert detail.json()["result"]["id"] == str(assets[0].id)
        download = client.get(
            f"/api/v1/assets/{assets[0].id}/download", follow_redirects=False
        )
        assert download.status_code == 307
        assert winning_candidate.object_key in download.headers["location"]
        assert stale_key not in download.headers["location"]
        assert winning_execute.execute(task_id).executed is False
    finally:
        for object_key in object_keys:
            minio.remove_object(bucket, object_key)
        _cleanup_task(factory, task_id)


def test_expired_lease_between_candidate_write_and_publish_leaves_no_result_pointer() -> None:
    _require_minio_integration()
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    factory = create_session_factory(database_url)
    store = MinioResultAssetStore()
    store.check_ready()
    minio = _minio_client()
    bucket = os.environ.get("MINIO_BUCKET", "museflow-results")
    clock = MutableClock()
    task_id = CreateTask(factory, clock=clock).execute(
        CreateTaskRequest(prompt="lease expires after candidate"), f"expired-candidate-{uuid4()}"
    ).task.id
    candidate_key: str | None = None

    try:
        execute = ExecuteGenerationAttempt(
            factory, MockProvider(), asset_store=store, clock=clock, lease_seconds=10
        )
        claim = execute._claim(task_id)
        assert claim is not None
        result = _generation_result(_png((1, 2, 3)))
        candidate = execute._store_result(claim, result)
        assert candidate is not None
        candidate_key = candidate.object_key
        clock.now += timedelta(seconds=11)

        assert execute._succeed(claim, result, candidate) is PublicationDecision.OWNERSHIP_LOST
        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
        assert task is not None and task.status == TaskStatus.RUNNING.value
        assert assets == []
        assert _read_object(minio, bucket, candidate.object_key) == result.content
    finally:
        if candidate_key is not None:
            minio.remove_object(bucket, candidate_key)
        _cleanup_task(factory, task_id)


def test_database_publication_failure_does_not_mark_task_succeeded_and_recovers_same_attempt(
) -> None:
    _require_minio_integration()
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    factory = create_session_factory(database_url)
    store = MinioResultAssetStore()
    store.check_ready()
    minio = _minio_client()
    bucket = os.environ.get("MINIO_BUCKET", "museflow-results")
    clock = MutableClock()
    task_id = CreateTask(factory, clock=clock).execute(
        CreateTaskRequest(prompt="database publication recovery"), f"db-publish-{uuid4()}"
    ).task.id
    candidate_keys: set[str] = set()

    def fail_success_commit(session) -> None:
        if any(
            isinstance(item, TaskEventModel) and item.event_type == "TASK_SUCCEEDED"
            for item in session.new
        ):
            raise RuntimeError("simulated database publication outage")

    try:
        event.listen(factory, "before_commit", fail_success_commit)
        try:
            execute = ExecuteGenerationAttempt(
                factory, MockProvider(), asset_store=store, clock=clock, lease_seconds=10
            )
            with pytest.raises(RuntimeError, match="database publication outage"):
                execute.execute(task_id)
        finally:
            event.remove(factory, "before_commit", fail_success_commit)

        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt = session.scalar(
                select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
        assert task is not None and task.status == TaskStatus.RUNNING.value
        assert attempt is not None and attempt.phase == "RESULT_PERSISTING"
        assert assets == []
        assert task is not None and attempt is not None
        result = MockProvider().generate(
            GenerationRequest(prompt=task.prompt, size_preset=task.size_preset),
            request_key=attempt.provider_request_key,
            remote_request_id=attempt.provider_request_id,
        )
        identity = result_identity(result.content, result.content_type)
        candidate_keys.add(
            candidate_object_key(task_id, attempt.id, identity)
        )
        assert _read_object(minio, bucket, next(iter(candidate_keys))) == result.content

        clock.now += timedelta(seconds=11)
        assert RecoverExpiredLeases(factory, clock=clock).run_once() == 1
        assert execute.execute(task_id).succeeded is True
        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert len(attempts) == 1
        assert len(assets) == 1
        assert assets[0].object_key == next(iter(candidate_keys))
    finally:
        for object_key in candidate_keys:
            minio.remove_object(bucket, object_key)
        _cleanup_task(factory, task_id)
