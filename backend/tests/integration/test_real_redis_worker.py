from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory
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
    task_id = (
        CreateTask(factory)
        .execute(CreateTaskRequest(prompt="real redis worker"), f"real-redis-{uuid4()}")
        .task.id
    )
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        publisher = CeleryTaskPublisher(create_celery_app(redis_url))
        assert OutboxDispatcher(factory, publisher).dispatch_once(limit=1000) >= 1

        deadline = time.monotonic() + 10
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
        assert len(attempts) == 1
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
