from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

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
