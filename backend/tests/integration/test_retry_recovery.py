from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from museflow.assets import StoredAsset
from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory
from museflow.providers import MockProvider, PermanentProviderError, TransientProviderError
from museflow.tasks.application import CreateTask, ManualRetryNotAllowedError
from museflow.tasks.domain import CreateTaskRequest, TaskPolicy, TaskStatus
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.recovery import RecoverExpiredLeases, ScheduleDueRetries


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
        assert execute._succeed(old_claim, result, None) is False
        assert execute._succeed(new_claim, result, None) is True
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_manual_retry_creates_one_linear_child(isolated_session_factory) -> None:
    clock = MutableClock()
    task_id = _create_task(isolated_session_factory, prompt="manual retry", clock=clock)
    try:
        with isolated_session_factory.begin() as session:
            task = session.get(GenerationTaskModel, task_id)
            assert task is not None
            task.status = TaskStatus.FAILED.value
            task.error_code = "RETRY_EXHAUSTED"
            task.error_message = "automatic retries exhausted"
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
    ):
        self.calls += 1
        if self.calls == 1:
            if self.record_remote_request_id and on_remote_request_id is not None:
                on_remote_request_id("remote-request-1")
            raise SystemExit("simulated worker crash")
        return MockProvider().generate(
            request,
            request_key=request_key,
            remote_request_id=remote_request_id,
        )


class CrashAfterUploadStore:
    def __init__(self) -> None:
        self.puts = 0
        self.written_keys: set[str] = set()

    def put_result(self, *, task_id, attempt_id, content: bytes, content_type: str) -> StoredAsset:
        self.puts += 1
        stored = StoredAsset(
            object_key=f"results/{task_id}/{attempt_id}/0.png",
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
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


def test_worker_crash_after_object_upload_reuses_deterministic_asset(
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
