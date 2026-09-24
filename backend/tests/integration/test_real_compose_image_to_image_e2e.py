from __future__ import annotations

import hashlib
import io
import os
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select

from museflow.api.app import create_app
from museflow.assets import MinioResultAssetStore
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ReferenceAssetModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.providers import (
    ImageToImageProviderInput,
    MockProvider,
    ProviderGenerationRequest,
)
from museflow.reference_assets.access import ReferenceAssetReader
from museflow.reference_assets.blob_store import MinioBlobStore
from museflow.tasks.domain import GenerationType, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (512, 512), (46, 107, 166)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pixel_digest(content: bytes) -> str:
    with Image.open(io.BytesIO(content)) as image:
        return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def test_real_compose_api_upload_to_mock_image_to_image_result() -> None:
    if os.environ.get("MUSEFLOW_RUN_REAL_IMAGE_TO_IMAGE_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REAL_IMAGE_TO_IMAGE_TEST=1 with isolated Compose services")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")

    factory = create_session_factory(database_url)
    reference_store = MinioBlobStore()
    result_store = MinioResultAssetStore()
    reference_store.check_ready()
    result_store.check_ready()
    client = TestClient(
        create_app(
            session_factory=factory,
            asset_store=result_store,
            reference_blob_store=reference_store,
        )
    )
    reference_id: UUID | None = None
    task_id: UUID | None = None
    reference_key: str | None = None
    result_key: str | None = None
    try:
        content = _png()
        upload = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": f"compose-i2i-upload-{uuid4()}"},
            files={"file": ("reference.png", content, "image/png")},
        )
        assert upload.status_code == 201
        assert upload.json()["status"] == "READY"
        reference_id = UUID(upload.json()["asset_id"])

        created = client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": f"compose-i2i-task-{uuid4()}"},
            json={
                "generation_type": "IMAGE_TO_IMAGE",
                "prompt": "paint this as a mountain at blue hour",
                "size_preset": "1280*1280",
                "reference_asset_id": str(reference_id),
            },
        )
        assert created.status_code == 201
        task_id = UUID(created.json()["id"])

        deadline = time.monotonic() + 60
        status = TaskStatus.QUEUED.value
        while time.monotonic() < deadline:
            with factory() as session:
                status = session.scalar(
                    select(GenerationTaskModel.status).where(GenerationTaskModel.id == task_id)
                )
            if status in {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value}:
                break
            time.sleep(0.25)
        assert status == TaskStatus.SUCCEEDED.value

        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt_rows = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
            result = session.scalar(
                select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
            )
            outbox = session.scalar(
                select(OutboxMessageModel).where(
                    OutboxMessageModel.aggregate_id == task_id,
                    OutboxMessageModel.message_type == "EXECUTE_TASK",
                )
            )
            reference = session.get(ReferenceAssetModel, reference_id)
        assert task is not None
        assert reference is not None and reference.status == "READY"
        assert task.generation_type == GenerationType.IMAGE_TO_IMAGE.value
        assert task.reference_asset_id == reference_id
        assert task.reference_sha256 == hashlib.sha256(content).hexdigest()
        assert task.provider_profile == "mock-image-generation-v2"
        assert outbox is not None and outbox.published_at is not None
        assert len(attempt_rows) == 1
        assert attempt_rows[0].phase == "COMPLETED"
        assert attempt_rows[0].status == "SUCCEEDED"
        assert attempt_rows[0].provider_request_id is not None
        assert result is not None
        assert result.role == "RESULT"
        assert task.reference_sha256 == hashlib.sha256(
            reference_store.get(reference.object_key)
        ).hexdigest()
        assert result.width == 1280 and result.height == 1280
        reference_key = reference.object_key
        result_key = result.object_key

        verified = ReferenceAssetReader(factory, reference_store).read_for_task(
            reference_id,
            expected_sha256=task.reference_sha256,
            ownership_guard=lambda: None,
        )
        expected = MockProvider().generate(
            ProviderGenerationRequest(
                input=ImageToImageProviderInput(
                    prompt=task.prompt,
                    size_preset=task.size_preset,
                    reference_image=verified,
                ),
                deadline_at=task.deadline_at,
            ),
            request_key=attempt_rows[0].provider_request_key,
            remote_request_id=attempt_rows[0].provider_request_id,
        )
        actual_content = reference_store.get(result.object_key)
        assert hashlib.sha256(actual_content).hexdigest() == result.sha256
        assert _pixel_digest(expected.content) == _pixel_digest(actual_content)

        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["generation_type"] == "IMAGE_TO_IMAGE"
        assert detail.json()["reference_download_url"] == f"/api/v1/assets/{reference_id}/download"
        assert detail.json()["result"]["download_url"] == f"/api/v1/assets/{result.id}/download"
        download = client.get(detail.json()["result"]["download_url"], follow_redirects=False)
        assert download.status_code == 307
        unsigned_url = urlsplit(download.headers["location"])._replace(
            query="", fragment=""
        ).geturl()
        with httpx.Client(timeout=10) as anonymous_client:
            assert anonymous_client.get(unsigned_url).status_code == 403
        history = client.get(
            "/api/v1/tasks",
            params={"generation_type": "IMAGE_TO_IMAGE", "status": "SUCCEEDED"},
        )
        assert history.status_code == 200
        assert str(task_id) in {item["id"] for item in history.json()["items"]}
    finally:
        if result_key is not None:
            reference_store.delete(result_key)
        if reference_key is not None:
            reference_store.delete(reference_key)
        if task_id is not None or reference_id is not None:
            with factory.begin() as session:
                aggregate_ids = [value for value in (task_id, reference_id) if value is not None]
                session.execute(
                    delete(OutboxMessageModel).where(
                        OutboxMessageModel.aggregate_id.in_(aggregate_ids)
                    )
                )
                if task_id is not None:
                    session.execute(
                        delete(TaskEventModel).where(TaskEventModel.task_id == task_id)
                    )
                    session.execute(
                        delete(GenerationAttemptModel).where(
                            GenerationAttemptModel.task_id == task_id
                        )
                    )
                    session.execute(
                        delete(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                    )
                    session.execute(
                        delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id)
                    )
                if reference_id is not None:
                    session.execute(
                        delete(ReferenceAssetModel).where(ReferenceAssetModel.id == reference_id)
                    )
        client.close()
