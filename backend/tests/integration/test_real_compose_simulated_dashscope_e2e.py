from __future__ import annotations

import hashlib
import io
import os
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select

from museflow.api.app import create_app
from museflow.assets import MinioResultAssetStore
from museflow.dashscope_image_adapter import (
    DASHSCOPE_IMAGE_RESULT_HOSTS,
    DashScopeWan26ImageAdapter,
)
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ReferenceAssetModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.queue import CeleryMaintenancePublisher, CeleryTaskPublisher
from museflow.reference_assets.blob_store import MinioBlobStore
from museflow.reference_assets.dispatch import (
    ReferenceMaintenanceOutboxDispatcher,
    ReferenceMaintenanceScheduler,
)
from museflow.safe_artifacts import SafeArtifactFetcher
from museflow.scheduler import run_scheduler_iteration
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.domain import GenerationType, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.recovery import RecoverExpiredLeases, ScheduleDueRetries


def _png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def test_simulated_dashscope_edit_runs_through_real_compose_services(monkeypatch) -> None:
    if os.environ.get("MUSEFLOW_RUN_SIMULATED_DASHSCOPE_E2E") != "1":
        pytest.skip("set MUSEFLOW_RUN_SIMULATED_DASHSCOPE_E2E=1 with isolated Compose services")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")

    monkeypatch.setenv("MUSEFLOW_DATABASE_URL", database_url)
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "simulated-provider-test-key")
    monkeypatch.setenv("DASHSCOPE_API_HOST", "simulated.cn-beijing.maas.aliyuncs.com")
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

    provider_requests: list[httpx.Request] = []
    result_fetch_count = 0
    result_bytes = _png((1_280, 1_280), (26, 101, 173))
    result_host = next(iter(DASHSCOPE_IMAGE_RESULT_HOSTS))
    result_url = f"https://{result_host}/simulated/result.png?signature=test-only"

    def provider_service(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"output": {"task_id": "simulated-remote-task", "task_status": "PENDING"}},
            )
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [{"message": {"content": [{"type": "image", "image": result_url}]}}],
                }
            },
        )

    def result_service(_: httpx.Request) -> httpx.Response:
        nonlocal result_fetch_count
        result_fetch_count += 1
        return httpx.Response(200, headers={"content-type": "image/png"}, content=result_bytes)

    def simulated_provider_factory(**values: object) -> DashScopeWan26ImageAdapter:
        assert values["profile_id"] == "dashscope-wan2.6-image-cn-beijing-edit"
        assert values["generation_type"] is GenerationType.IMAGE_TO_IMAGE
        return DashScopeWan26ImageAdapter(
            api_key="simulated-provider-test-key",
            api_host="simulated.cn-beijing.maas.aliyuncs.com",
            client=httpx.Client(transport=httpx.MockTransport(provider_service)),
            fetcher=SafeArtifactFetcher(
                resolver=lambda _: ["93.184.216.34"],
                transport_factory=lambda *_: httpx.MockTransport(result_service),
            ),
            poll_interval_seconds=0,
            total_timeout_seconds=30,
            sleep=lambda _: None,
        )

    import museflow.worker as worker_module

    monkeypatch.setattr(worker_module, "create_provider_for_task", simulated_provider_factory)
    worker_app = worker_module.celery_app
    reference_id: UUID | None = None
    task_id: UUID | None = None
    reference_key: str | None = None
    result_key: str | None = None
    try:
        upload = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": f"simulated-dashscope-upload-{uuid4()}"},
            files={"file": ("reference.png", _png((512, 512), (46, 107, 166)), "image/png")},
        )
        assert upload.status_code == 201
        assert upload.json()["status"] == "READY"
        reference_id = UUID(upload.json()["asset_id"])

        created = client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": f"simulated-dashscope-task-{uuid4()}"},
            json={
                "generation_type": GenerationType.IMAGE_TO_IMAGE.value,
                "prompt": "Repaint the reference as a blue-hour watercolor landscape.",
                "size_preset": "1280*1280",
                "reference_asset_id": str(reference_id),
            },
        )
        assert created.status_code == 201
        task_id = UUID(created.json()["id"])

        celery_publisher = CeleryTaskPublisher(worker_app)
        run_scheduler_iteration(
            ScheduleDueRetries(factory),
            RecoverExpiredLeases(factory),
            OutboxDispatcher(factory, celery_publisher),
            ReferenceMaintenanceScheduler(factory),
            ReferenceMaintenanceOutboxDispatcher(factory, CeleryMaintenancePublisher(worker_app)),
        )
        with start_worker(
            worker_app,
            pool="solo",
            queues=["generation"],
            perform_ping_check=False,
            loglevel="error",
        ):
            # Dispatch once more after the worker is ready; outbox publishing is idempotent.
            run_scheduler_iteration(
                ScheduleDueRetries(factory),
                RecoverExpiredLeases(factory),
                OutboxDispatcher(factory, celery_publisher),
                ReferenceMaintenanceScheduler(factory),
                ReferenceMaintenanceOutboxDispatcher(
                    factory, CeleryMaintenancePublisher(worker_app)
                ),
            )
            deadline = time.monotonic() + 60
            status = TaskStatus.QUEUED.value
            while time.monotonic() < deadline:
                with factory() as session:
                    status = session.scalar(
                        select(GenerationTaskModel.status).where(GenerationTaskModel.id == task_id)
                    )
                if status in {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value}:
                    break
                time.sleep(0.2)
        if status != TaskStatus.SUCCEEDED.value:
            with factory() as session:
                failed_task = session.get(GenerationTaskModel, task_id)
                failed_attempts = list(
                    session.scalars(
                        select(GenerationAttemptModel)
                        .where(GenerationAttemptModel.task_id == task_id)
                        .order_by(GenerationAttemptModel.sequence.desc())
                    )
                )
            latest_attempt = failed_attempts[0] if failed_attempts else None
            failure_summary = {
                "status": status,
                "task_error_code": failed_task.error_code if failed_task is not None else None,
                "attempt_phase": latest_attempt.phase if latest_attempt is not None else None,
                "attempt_status": latest_attempt.status if latest_attempt is not None else None,
                "attempt_error_code": (
                    latest_attempt.error_code if latest_attempt is not None else None
                ),
                "provider_methods": [request.method for request in provider_requests],
                "result_fetch_count": result_fetch_count,
            }
            pytest.fail(f"simulated DashScope task did not succeed: {failure_summary}")

        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            reference = session.get(ReferenceAssetModel, reference_id)
            attempt_rows = list(
                session.scalars(
                    select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
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
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert task.generation_type == GenerationType.IMAGE_TO_IMAGE.value
        assert task.provider_profile == "dashscope-wan2.6-image-cn-beijing-edit"
        assert reference is not None and reference.status == "READY"
        assert outbox is not None and outbox.published_at is not None
        assert len(attempt_rows) == 1
        assert attempt_rows[0].status == "SUCCEEDED"
        assert attempt_rows[0].phase == "COMPLETED"
        assert attempt_rows[0].provider_request_id == "simulated-remote-task"
        assert result is not None and result.role == "RESULT"
        assert result.width == 1_280 and result.height == 1_280
        assert len([request for request in provider_requests if request.method == "POST"]) == 1
        assert [request.method for request in provider_requests] == ["POST", "GET"]
        assert result_fetch_count == 1
        assert result_url not in repr(task)
        assert result_url not in repr(attempt_rows[0])
        reference_key = reference.object_key
        result_key = result.object_key

        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["result"]["download_url"] == f"/api/v1/assets/{result.id}/download"
        redirect = client.get(detail.json()["result"]["download_url"], follow_redirects=False)
        assert redirect.status_code == 307
        with httpx.Client(timeout=10, follow_redirects=False) as anonymous_client:
            stored = anonymous_client.get(redirect.headers["location"])
            assert stored.status_code == 200
            assert hashlib.sha256(stored.content).hexdigest() == result.sha256
            unsigned_url = (
                urlsplit(redirect.headers["location"])._replace(query="", fragment="").geturl()
            )
            assert anonymous_client.get(unsigned_url).status_code == 403
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
                    session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
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
