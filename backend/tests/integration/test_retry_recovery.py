from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from museflow.assets import StoredAsset, candidate_object_key, result_identity
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.providers import (
    LeaseChecker,
    MockProvider,
    PermanentProviderError,
    TransientProviderError,
)
from museflow.tasks.application import CreateTask, ManualRetryNotAllowedError
from museflow.tasks.domain import CreateTaskRequest, TaskPolicy, TaskStatus
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.execution_semantics import AttemptPhase
from museflow.tasks.recovery import RecoverExpiredLeases, ScheduleDueRetries
from museflow.tasks.result_publication import PublicationDecision


@pytest.fixture()
def isolated_session_factory():
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_session_factory(database_url)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _create_task(
    factory, *, prompt: str, policy: TaskPolicy | None = None, clock: MutableClock | None = None
) -> UUID:
    return CreateTask(factory, policy=policy, clock=clock).execute(
        CreateTaskRequest(prompt=prompt), f"window3-{uuid4()}"
    ).task.id


def _cleanup(factory, task_id: UUID) -> None:
    with factory.begin() as session:
        session.execute(
            delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
        )
        session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
        session.execute(delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id))


def test_transient_errors_are_retried_then_succeed_without_new_business_task(
    isolated_session_factory,
) -> None:
    clock = MutableClock()
    task_id = _create_task(isolated_session_factory, prompt="transient recovery", clock=clock)
    provider = MockProvider(
        failures=[TransientProviderError(), TransientProviderError()]
    )
    execute = ExecuteGenerationAttempt(
        isolated_session_factory, provider, clock=clock, random_source=lambda: 0.0
    )
    try:
        assert execute.execute(task_id).succeeded is False
        assert ScheduleDueRetries(isolated_session_factory, clock=clock).run_once() == 1
        assert execute.execute(task_id).succeeded is False
        assert ScheduleDueRetries(isolated_session_factory, clock=clock).run_once() == 1
        assert execute.execute(task_id).succeeded is True
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task_id)
                    .order_by(GenerationAttemptModel.sequence)
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert [attempt.sequence for attempt in attempts] == [1, 2, 3]
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_permanent_error_does_not_enter_retry_wait(isolated_session_factory) -> None:
    clock = MutableClock()
    task_id = _create_task(isolated_session_factory, prompt="permanent failure", clock=clock)
    execute = ExecuteGenerationAttempt(
        isolated_session_factory,
        MockProvider(failures=[PermanentProviderError()]),
        clock=clock,
    )
    try:
        assert execute.execute(task_id).succeeded is False
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
        assert task is not None
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "PROVIDER_REJECTED"
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_expired_worker_token_cannot_commit_after_takeover(isolated_session_factory) -> None:
    clock = MutableClock()
    task_id = _create_task(isolated_session_factory, prompt="fencing", clock=clock)
    provider = MockProvider()
    execute = ExecuteGenerationAttempt(
        isolated_session_factory, provider, clock=clock, lease_seconds=10
    )
    try:
        old_claim = execute._claim(task_id)
        assert old_claim is not None
        clock.now += timedelta(seconds=11)
        assert RecoverExpiredLeases(isolated_session_factory, clock=clock).run_once() == 1
        new_claim = execute._claim(task_id)
        assert new_claim is not None
        result = provider.generate(
            old_claim.request,
            request_key=old_claim.provider_request_key,
            remote_request_id=None,
        )
        assert execute._succeed(old_claim, result, None) is PublicationDecision.OWNERSHIP_LOST
        assert execute._succeed(new_claim, result, None) is PublicationDecision.PUBLISHED
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
    finally:
        _cleanup(isolated_session_factory, task_id)


@pytest.mark.parametrize(
    "error_code",
    ["RETRY_EXHAUSTED", "PROVIDER_NOT_CONFIGURED", "PROVIDER_AUTHENTICATION"],
)
def test_manual_retry_creates_one_linear_child(isolated_session_factory, error_code: str) -> None:
    clock = MutableClock()
    task_id = _create_task(isolated_session_factory, prompt="manual retry", clock=clock)
    try:
        with isolated_session_factory.begin() as session:
            task = session.get(GenerationTaskModel, task_id)
            assert task is not None
            task.status = TaskStatus.FAILED.value
            task.error_code = error_code
            task.error_message = f"failed with {error_code}"
        create = CreateTask(isolated_session_factory, clock=clock)
        child = create.execute(
            CreateTaskRequest(prompt="manual retry"),
            "manual-retry-key",
            retried_from_task_id=task_id,
        )
        assert child.task.retried_from_task_id == task_id
        with pytest.raises(ManualRetryNotAllowedError):
            create.execute(
                CreateTaskRequest(prompt="manual retry"),
                "manual-retry-key-2",
                retried_from_task_id=task_id,
            )
    finally:
        _cleanup(isolated_session_factory, child.task.id if "child" in locals() else task_id)
        _cleanup(isolated_session_factory, task_id)


class CrashOnceProvider:
    name = "crash-once"

    def __init__(self, *, record_remote_request_id: bool) -> None:
        self.record_remote_request_id = record_remote_request_id
        self.calls = 0

    def generate(
        self,
        request,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id=None,
        on_phase=None,
        lease_guard: LeaseChecker | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            if self.record_remote_request_id and on_remote_request_id is not None:
                if on_phase is not None:
                    on_phase(AttemptPhase.PROVIDER_SUBMITTING)
                on_remote_request_id("remote-request-1")
            raise SystemExit("simulated worker crash")
        return MockProvider().generate(
            request,
            request_key=request_key,
            remote_request_id=remote_request_id,
            on_remote_request_id=on_remote_request_id,
            on_phase=on_phase,
            lease_guard=lease_guard,
        )


class FailingResultStore:
    def __init__(self, failures: int | None) -> None:
        self.failures = failures
        self.calls = 0

    def put_result(self, *, task_id, attempt_id, content: bytes, content_type: str) -> StoredAsset:
        self.calls += 1
        if self.failures is None or self.calls <= self.failures:
            raise RuntimeError("simulated object store outage")
        identity = result_identity(content, content_type)
        return StoredAsset(
            object_key=candidate_object_key(task_id, attempt_id, identity),
            content_type=identity.content_type,
            size_bytes=identity.size_bytes,
            sha256=identity.sha256,
        )

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
        raise NotImplementedError

    def check_ready(self) -> None:
        return None


def test_result_storage_failure_reuses_same_attempt_and_recovers(
    isolated_session_factory,
) -> None:
    clock = _current_clock()
    task_id = _create_task(isolated_session_factory, prompt="storage recovery", clock=clock)
    store = FailingResultStore(failures=1)
    execute = ExecuteGenerationAttempt(
        isolated_session_factory,
        MockProvider(),
        asset_store=store,
        clock=clock,
        lease_seconds=10,
    )
    try:
        with pytest.raises(RuntimeError, match="simulated object store outage"):
            execute.execute(task_id)
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt = session.scalar(
                select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
            events = list(
                session.scalars(
                    select(TaskEventModel)
                    .where(TaskEventModel.task_id == task_id)
                    .order_by(TaskEventModel.created_at, TaskEventModel.id)
                )
            )
        assert task is not None and task.status == TaskStatus.RUNNING.value
        assert task.error_code == "RESULT_STORAGE_ERROR"
        assert attempt is not None
        assert attempt.sequence == 1
        assert attempt.status == "RUNNING"
        assert attempt.phase == "RESULT_PERSISTING"
        assert attempt.error_code == "RESULT_STORAGE_ERROR"
        assert assets == []
        assert {event.event_type for event in events} == {
            "TASK_QUEUED",
            "ATTEMPT_STARTED",
            "ATTEMPT_PHASE_CHANGED",
            "RESULT_STORAGE_FAILED",
        }

        clock.now += timedelta(seconds=11)
        assert RecoverExpiredLeases(isolated_session_factory, clock=clock).run_once() == 1
        assert execute.execute(task_id).succeeded is True

        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task_id)
                    .order_by(GenerationAttemptModel.sequence)
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert len(attempts) == 1
        assert attempts[0].status == "SUCCEEDED"
        assert attempts[0].phase == "COMPLETED"
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_result_storage_failure_keeps_diagnostic_code_at_deadline(
    isolated_session_factory,
) -> None:
    clock = _current_clock()
    task_id = _create_task(
        isolated_session_factory,
        prompt="storage deadline",
        policy=TaskPolicy(max_attempts=3, policy_version="test", deadline_seconds=15),
        clock=clock,
    )
    execute = ExecuteGenerationAttempt(
        isolated_session_factory,
        MockProvider(),
        asset_store=FailingResultStore(failures=None),
        clock=clock,
        lease_seconds=5,
    )
    try:
        with pytest.raises(RuntimeError):
            execute.execute(task_id)
        clock.now += timedelta(seconds=6)
        assert RecoverExpiredLeases(isolated_session_factory, clock=clock).run_once() == 1
        with pytest.raises(RuntimeError):
            execute.execute(task_id)
        clock.now += timedelta(seconds=6)
        assert RecoverExpiredLeases(isolated_session_factory, clock=clock).run_once() == 1
        with pytest.raises(RuntimeError):
            execute.execute(task_id)
        clock.now += timedelta(seconds=6)
        assert RecoverExpiredLeases(isolated_session_factory, clock=clock).run_once() == 1

        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt = session.scalar(
                select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
        assert task is not None and task.status == TaskStatus.FAILED.value
        assert task.error_code == "DEADLINE_EXCEEDED"
        assert attempt is not None
        assert attempt.status == "FAILED"
        assert attempt.phase == "RESULT_PERSISTING"
        assert attempt.error_code == "DEADLINE_EXCEEDED"
    finally:
        _cleanup(isolated_session_factory, task_id)


class CrashAfterUploadStore:
    def __init__(self) -> None:
        self.puts = 0
        self.written_keys: set[str] = set()

    def put_result(self, *, task_id, attempt_id, content: bytes, content_type: str) -> StoredAsset:
        self.puts += 1
        identity = result_identity(content, content_type)
        stored = StoredAsset(
            object_key=candidate_object_key(task_id, attempt_id, identity),
            content_type=identity.content_type,
            size_bytes=identity.size_bytes,
            sha256=identity.sha256,
        )
        self.written_keys.add(stored.object_key)
        if self.puts == 1:
            raise RuntimeError("simulated worker crash after object upload")
        return stored

    def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
        raise NotImplementedError

    def check_ready(self) -> None:
        return None


def _current_clock() -> MutableClock:
    clock = MutableClock()
    clock.now = datetime.now(UTC)
    return clock

def _recover_after_crash(factory, task_id: UUID, clock: MutableClock) -> None:
    clock.now += timedelta(seconds=11)
    assert RecoverExpiredLeases(factory, clock=clock).run_once() == 1


def test_worker_crash_before_provider_call_reuses_unfinished_attempt(
    isolated_session_factory,
) -> None:
    clock = _current_clock()
    task_id = _create_task(isolated_session_factory, prompt="crash before provider", clock=clock)
    provider = CrashOnceProvider(record_remote_request_id=False)
    execute = ExecuteGenerationAttempt(
        isolated_session_factory, provider, clock=clock, lease_seconds=10
    )
    try:
        with pytest.raises(SystemExit):
            execute.execute(task_id)
        _recover_after_crash(isolated_session_factory, task_id, clock)
        assert execute.execute(task_id).succeeded is True
        with isolated_session_factory() as session:
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
                )
            )
        assert len(attempts) == 1
        assert attempts[0].sequence == 1
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_worker_crash_after_provider_acceptance_reuses_remote_request_id(
    isolated_session_factory,
) -> None:
    clock = _current_clock()
    task_id = _create_task(isolated_session_factory, prompt="crash after acceptance", clock=clock)
    provider = CrashOnceProvider(record_remote_request_id=True)
    execute = ExecuteGenerationAttempt(
        isolated_session_factory, provider, clock=clock, lease_seconds=10
    )
    try:
        with pytest.raises(SystemExit):
            execute.execute(task_id)
        _recover_after_crash(isolated_session_factory, task_id, clock)
        assert execute.execute(task_id).succeeded is True
        with isolated_session_factory() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
        assert attempt is not None
        assert attempt.sequence == 1
        assert attempt.provider_request_id == "remote-request-1"
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_worker_crash_after_object_upload_reuses_same_content_candidate(
    isolated_session_factory,
) -> None:
    clock = _current_clock()
    task_id = _create_task(isolated_session_factory, prompt="crash after upload", clock=clock)
    store = CrashAfterUploadStore()
    execute = ExecuteGenerationAttempt(
        isolated_session_factory,
        MockProvider(),
        asset_store=store,
        clock=clock,
        lease_seconds=10,
    )
    try:
        with pytest.raises(RuntimeError):
            execute.execute(task_id)
        _recover_after_crash(isolated_session_factory, task_id, clock)
        assert execute.execute(task_id).succeeded is True
        assert store.puts == 2
        assert len(store.written_keys) == 1
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED.value
    finally:
        _cleanup(isolated_session_factory, task_id)
