from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory
from museflow.providers import MockProvider
from museflow.queue import CeleryTaskPublisher, create_celery_app
from museflow.tasks.application import CreateTask
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.domain import CreateTaskRequest, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel


def test_real_redis_scheduler_worker_path() -> None:
    if os.environ.get("MUSEFLOW_RUN_REAL_REDIS_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REAL_REDIS_TEST=1 with Compose scheduler and worker")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")

    factory = create_session_factory(database_url)
    lease_seconds = float(os.environ.get("MUSEFLOW_LEASE_SECONDS", "240"))
    if lease_seconds >= MockProvider.LONG_RUNNING_DELAY_SECONDS:
        pytest.skip("set MUSEFLOW_LEASE_SECONDS below the long-running mock duration")
    task_id = (
        CreateTask(factory)
        .execute(
            CreateTaskRequest(
                prompt="real redis worker",
                execution_profile="long_running_success",
            ),
            f"real-redis-{uuid4()}",
        )
        .task.id
    )
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        publisher = CeleryTaskPublisher(create_celery_app(redis_url))
        assert OutboxDispatcher(factory, publisher).dispatch_once(limit=1000) >= 1
        publisher.publish(task_id)

        deadline = time.monotonic() + 20
        status = TaskStatus.QUEUED.value
        while time.monotonic() < deadline:
            with factory() as session:
                status = session.scalar(
                    select(GenerationTaskModel.status).where(GenerationTaskModel.id == task_id)
                )
            if status in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value):
                break
            time.sleep(0.25)

        assert status == TaskStatus.SUCCEEDED.value
        with factory() as session:
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
                )
            )
            events = list(
                session.scalars(
                    select(TaskEventModel).where(TaskEventModel.task_id == task_id)
                )
            )
            task = session.get(GenerationTaskModel, task_id)
        assert len(attempts) == 1
        assert attempts[0].phase == "COMPLETED"
        assert attempts[0].provider_request_id is not None
        assert task is not None and task.completed_at is not None
        assert (task.completed_at - attempts[0].started_at).total_seconds() > lease_seconds
        assert all(event.event_type != "ATTEMPT_RECLAIMED" for event in events)
    finally:
        with factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
            )
            session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
            session.execute(
                delete(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
            session.execute(delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id))
