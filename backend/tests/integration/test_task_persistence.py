from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ReferenceAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.tasks.application import (
    CreateTask,
    GetTaskDetail,
    IdempotencyConflictError,
    ListTasks,
    TaskNotFoundError,
)
from museflow.tasks.domain import (
    CreateTaskRequest,
    GenerationType,
    ReferenceAssetStatus,
    TaskStatus,
)
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


def test_image_to_image_creation_is_atomic_and_history_filter_keeps_cursor_order(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "mock")
    asset_id = UUID("00000000-0000-0000-0000-000000000777")
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            ReferenceAssetModel(
                id=asset_id,
                idempotency_key="history-reference-asset",
                request_fingerprint="f" * 64,
                status=ReferenceAssetStatus.READY.value,
                object_key=f"references/{asset_id}/{'a' * 64}.png",
                content_type="image/png",
                size_bytes=512,
                width=512,
                height=512,
                sha256="a" * 64,
                created_at=now,
                ready_at=now,
            )
        )

    class FailingOutboxRepository(TaskRepository):
        def add_execution_outbox(self, session, task):
            raise RuntimeError("outbox unavailable")

    rollback_task_id = UUID("00000000-0000-0000-0000-000000000778")
    task_ids: list[UUID] = []
    try:
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            CreateTask(
                session_factory,
                repository=FailingOutboxRepository(),
                id_factory=lambda: rollback_task_id,
            ).execute(
                CreateTaskRequest(
                    prompt="atomic image edit",
                    generation_type=GenerationType.IMAGE_TO_IMAGE,
                    reference_asset_id=asset_id,
                ),
                "image-edit-rollback",
            )
        with session_factory() as session:
            assert session.get(GenerationTaskModel, rollback_task_id) is None
            assert session.scalar(
                select(OutboxMessageModel.id).where(
                    OutboxMessageModel.aggregate_id == rollback_task_id
                )
            ) is None
            assert session.scalar(
                select(TaskEventModel.id).where(TaskEventModel.task_id == rollback_task_id)
            ) is None

        instants = iter(now + timedelta(minutes=index) for index in range(4))
        create = CreateTask(session_factory, clock=lambda: next(instants))
        for index, generation_type in enumerate(
            (
                GenerationType.TEXT_TO_IMAGE,
                GenerationType.IMAGE_TO_IMAGE,
                GenerationType.TEXT_TO_IMAGE,
                GenerationType.IMAGE_TO_IMAGE,
            )
        ):
            task_ids.append(
                create.execute(
                    CreateTaskRequest(
                        prompt=f"history item {index}",
                        generation_type=generation_type,
                        reference_asset_id=(
                            asset_id if generation_type is GenerationType.IMAGE_TO_IMAGE else None
                        ),
                    ),
                    f"history-filter-{index}",
                ).task.id
            )

        listing = ListTasks(session_factory)
        first_page = listing.execute(
            limit=1,
            status=TaskStatus.QUEUED,
            generation_type=GenerationType.IMAGE_TO_IMAGE,
        )
        second_page = listing.execute(
            limit=1,
            cursor=first_page.next_cursor,
            status=TaskStatus.QUEUED,
            generation_type=GenerationType.IMAGE_TO_IMAGE,
        )
        all_types = listing.execute(limit=10, status=TaskStatus.QUEUED)

        assert [task.id for task in first_page.items] == [task_ids[3]]
        assert [task.id for task in second_page.items] == [task_ids[1]]
        assert second_page.next_cursor is None
        assert len(all_types.items) == 4
        assert {task.generation_type for task in all_types.items} == set(GenerationType)
    finally:
        with session_factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id.in_(task_ids))
            )
            session.execute(delete(TaskEventModel).where(TaskEventModel.task_id.in_(task_ids)))
            session.execute(
                delete(GenerationTaskModel).where(GenerationTaskModel.id.in_(task_ids))
            )
            session.execute(delete(ReferenceAssetModel).where(ReferenceAssetModel.id == asset_id))
