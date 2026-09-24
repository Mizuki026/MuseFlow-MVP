from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory
from museflow.providers import GenerationRequest, MockProvider, ProviderError
from museflow.tasks.application import CreateTask
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.domain import CreateTaskRequest, TaskStatus
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.repository import TaskRepository


@pytest.fixture()
def isolated_session_factory():
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_session_factory(database_url)


def _create_task(factory, prompt: str) -> UUID:
    result = CreateTask(factory).execute(
        CreateTaskRequest(prompt=prompt), f"window2-{UUID(int=__import__('secrets').randbits(128))}"
    )
    return result.task.id


def _cleanup(factory, task_id: UUID) -> None:
    with factory.begin() as session:
        session.execute(
            delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
        )
        session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
        session.execute(
            delete(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
        )
        session.execute(delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id))


class RecordingPublisher:
    def __init__(self) -> None:
        self.task_ids: list[UUID] = []

    def publish(self, task_id: UUID) -> None:
        self.task_ids.append(task_id)


class FailingPublisher:
    def publish(self, task_id: UUID) -> None:
        raise ConnectionError("redis unavailable")


def test_mock_provider_success_contract_is_deterministic() -> None:
    provider = MockProvider()
    request = GenerationRequest(prompt="a lighthouse", size_preset="1280*1280")

    first = provider.generate(request, request_key="stable-key", remote_request_id=None)
    second = provider.generate(request, request_key="stable-key", remote_request_id=None)

    assert first == second
    assert first.provider_name == "mock"
    assert first.result_digest == "mock-result-stable-key"


def test_mock_provider_scenarios_are_deterministic_by_attempt() -> None:
    request = GenerationRequest(prompt="recovery", size_preset="1280*1280")
    provider = MockProvider(scenario="transient_then_success")

    with pytest.raises(ProviderError) as first:
        provider.generate(request, request_key="task:attempt:1", remote_request_id=None)
    result = provider.generate(request, request_key="task:attempt:2", remote_request_id=None)

    assert first.value.code == "PROVIDER_UNAVAILABLE"
    assert result.provider_name == "mock"


def test_dispatcher_retries_unpublished_outbox_after_redis_failure(
    isolated_session_factory,
) -> None:
    task_id = _create_task(isolated_session_factory, "redis recovery")
    try:
        failed = OutboxDispatcher(
            isolated_session_factory, FailingPublisher(), clock=lambda: datetime.now(UTC)
        )
        assert failed.dispatch_once(limit=10) == 0
        with isolated_session_factory() as session:
            assert TaskRepository().list_outbox(session, task_id)[0].published_at is None

        publisher = RecordingPublisher()
        recovered = OutboxDispatcher(
            isolated_session_factory, publisher, clock=lambda: datetime.now(UTC)
        )
        assert recovered.dispatch_once(limit=10) == 1
        assert publisher.task_ids == [task_id]
    finally:
        _cleanup(isolated_session_factory, task_id)


def test_worker_success_and_duplicate_delivery_have_one_authoritative_attempt(
    isolated_session_factory,
) -> None:
    task_id = _create_task(isolated_session_factory, "worker success")
    try:
        execute = ExecuteGenerationAttempt(isolated_session_factory, MockProvider())

        first = execute.execute(task_id)
        duplicate = execute.execute(task_id)

        assert first.executed is True
        assert first.succeeded is True
        assert duplicate.executed is False
        with isolated_session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
                )
            )
            events = list(
                session.scalars(
                    select(TaskEventModel)
                    .where(TaskEventModel.task_id == task_id)
                    .order_by(TaskEventModel.created_at, TaskEventModel.id)
                )
            )
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED.value
        assert len(attempts) == 1
        assert [event.event_type for event in events] == [
            "TASK_QUEUED",
            "ATTEMPT_STARTED",
            "ATTEMPT_PHASE_CHANGED",
            "ATTEMPT_PHASE_CHANGED",
            "ATTEMPT_PHASE_CHANGED",
            "ATTEMPT_PHASE_CHANGED",
            "ATTEMPT_PHASE_CHANGED",
            "TASK_SUCCEEDED",
        ]
        assert [event.payload.get("phase") for event in events[2:-1]] == [
            "PROVIDER_SUBMITTING",
            "PROVIDER_RUNNING",
            "RESULT_FETCHING",
            "RESULT_PERSISTING",
            "COMPLETED",
        ]
    finally:
        _cleanup(isolated_session_factory, task_id)
