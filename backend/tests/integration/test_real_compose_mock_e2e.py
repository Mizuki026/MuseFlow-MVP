from __future__ import annotations

import hashlib
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from museflow.assets import MinioResultAssetStore
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.queue import CeleryTaskPublisher, create_celery_app
from museflow.tasks.application import CreateTask
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.domain import CreateTaskRequest, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel


def test_compose_mock_result_reaches_private_minio_and_signed_download() -> None:
    if os.environ.get("MUSEFLOW_RUN_REAL_COMPOSE_E2E_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REAL_COMPOSE_E2E_TEST=1 with Compose services")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")

    factory = create_session_factory(database_url)
    store = MinioResultAssetStore()
    store.check_ready()
    task_id = (
        CreateTask(factory)
        .execute(CreateTaskRequest(prompt="compose mock asset check"), f"mock-e2e-{uuid4()}")
        .task.id
    )
    asset_key: str | None = None
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        publisher = CeleryTaskPublisher(create_celery_app(redis_url))
        OutboxDispatcher(factory, publisher).dispatch_once(limit=1000)

        deadline = time.monotonic() + 30
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
            asset = session.scalar(
                select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
            )
            assert asset is not None
            asset_key = asset.object_key
            expected_digest = asset.sha256
            assert asset.content_type == "image/png"

        signed_url = store.presigned_download(asset_key, expires_seconds=5)
        unsigned_url = urlsplit(signed_url)._replace(query="", fragment="").geturl()
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            signed = client.get(signed_url)
            assert signed.status_code == 200
            assert hashlib.sha256(signed.content).hexdigest() == expected_digest
            assert client.get(unsigned_url).status_code == 403
            time.sleep(6)
            assert client.get(signed_url).status_code == 403
    finally:
        if asset_key:
            store._client.remove_object(store._bucket, asset_key)
        with factory.begin() as session:
            session.execute(delete(ResultAssetModel).where(ResultAssetModel.task_id == task_id))
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
            )
            session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
            session.execute(
                delete(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
            session.execute(delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id))
