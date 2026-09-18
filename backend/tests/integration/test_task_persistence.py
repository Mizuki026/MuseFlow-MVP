from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory
from museflow.tasks.application import (
    CreateTask,
    GetTaskDetail,
    IdempotencyConflictError,
    ListTasks,
    TaskNotFoundError,
)
from museflow.tasks.domain import CreateTaskRequest, TaskStatus
from museflow.tasks.repository import TaskRepository


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return value


@pytest.fixture()
def session_factory(database_url: str):
    factory = create_session_factory(database_url)
    with factory.begin() as session:
        session.query(OutboxMessageModel).delete()
        session.query(TaskEventModel).delete()
        session.query(GenerationTaskModel).delete()
    return factory


def test_create_persists_task_event_and_execution_outbox_in_one_transaction(
    session_factory,
) -> None:
    create = CreateTask(session_factory)
    result = create.execute(CreateTaskRequest(prompt="a mountain lake"), "integration-1")

    detail = GetTaskDetail(session_factory).execute(result.task.id)
    with session_factory() as session:
        outbox = TaskRepository().list_outbox(session, result.task.id)

    assert result.idempotency_replayed is False
    assert detail.task.status is TaskStatus.QUEUED
    assert [event.event_type for event in detail.events] == ["TASK_QUEUED"]
    assert len(outbox) == 1
    assert outbox[0].message_type == "EXECUTE_TASK"
    assert outbox[0].payload == {"task_id": str(result.task.id)}


def test_same_key_replays_without_another_event_or_outbox(session_factory) -> None:
    create = CreateTask(session_factory)
    first = create.execute(CreateTaskRequest(prompt="same request"), "integration-2")
    second = create.execute(CreateTaskRequest(prompt="same request"), "integration-2")

    assert second.idempotency_replayed is True
    assert second.task.id == first.task.id
    with session_factory() as session:
        assert len(TaskRepository().list_events(session, first.task.id)) == 1
        assert len(TaskRepository().list_outbox(session, first.task.id)) == 1


def test_same_key_with_different_request_is_a_conflict(session_factory) -> None:
    create = CreateTask(session_factory)
    create.execute(CreateTaskRequest(prompt="first request"), "integration-3")

    with pytest.raises(IdempotencyConflictError):
        create.execute(CreateTaskRequest(prompt="different request"), "integration-3")


def test_concurrent_same_key_creates_one_task_and_all_callers_read_the_winner(
    session_factory,
) -> None:
    def submit() -> tuple[UUID, bool]:
        result = CreateTask(session_factory).execute(
            CreateTaskRequest(prompt="concurrent request"), "integration-concurrent"
        )
        return result.task.id, result.idempotency_replayed

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: submit(), range(8)))

    assert {task_id for task_id, _ in results}.__len__() == 1
    assert sum(not replayed for _, replayed in results) == 1
    assert len(ListTasks(session_factory).execute().items) == 1


def test_failed_creation_transaction_leaves_no_partial_task(session_factory) -> None:
    class FailingOutboxRepository(TaskRepository):
        def add_execution_outbox(self, session, task):
            raise RuntimeError("outbox unavailable")

    create = CreateTask(session_factory, repository=FailingOutboxRepository())
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        create.execute(CreateTaskRequest(prompt="must roll back"), "integration-rollback")

    with pytest.raises(TaskNotFoundError):
        GetTaskDetail(session_factory).execute(
            ListTasks(session_factory).execute().items[0].id
            if ListTasks(session_factory).execute().items
            else UUID("00000000-0000-0000-0000-000000000000")
        )


def test_cursor_order_and_status_filter_are_stable(session_factory) -> None:
    start = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    times: Iterator[datetime] = iter(start + timedelta(minutes=index) for index in range(3))
    create = CreateTask(session_factory, clock=lambda: next(times))
    for index in range(3):
        create.execute(CreateTaskRequest(prompt=f"history {index}"), f"history-{index}")

    first_page = ListTasks(session_factory).execute(limit=2)
    second_page = ListTasks(session_factory).execute(limit=2, cursor=first_page.next_cursor)
    queued_page = ListTasks(session_factory).execute(limit=10, status=TaskStatus.QUEUED)

    assert [item.prompt for item in first_page.items] == ["history 2", "history 1"]
    assert [item.prompt for item in second_page.items] == ["history 0"]
    assert all(item.status is TaskStatus.QUEUED for item in queued_page.items)
