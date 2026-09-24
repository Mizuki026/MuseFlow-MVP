from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from museflow.api.app import create_app
from museflow.assets import StoredAsset, candidate_object_key, result_identity
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.providers import MockProvider
from museflow.tasks.application import CreateTask
from museflow.tasks.domain import CreateTaskRequest
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel


@pytest.fixture()
def client():
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for API tests")
    session_factory = create_session_factory(database_url)
    with session_factory.begin() as session:
        session.query(OutboxMessageModel).delete()
        session.query(TaskEventModel).delete()
        session.query(GenerationTaskModel).delete()
    return TestClient(create_app(session_factory=session_factory))


def test_create_task_returns_201_and_does_not_expose_internal_fields(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-1"},
        json={"prompt": "a red kite over the sea"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["idempotency_replayed"] is False
    assert "idempotency_key" not in body
    assert "request_fingerprint" not in body
    assert "version" not in body
    assert "outbox" not in body
    assert body["generation_type"] == "TEXT_TO_IMAGE"


def test_omitted_and_explicit_text_generation_type_keep_idempotency_compatible(
    client: TestClient,
) -> None:
    first = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-text-default"},
        json={"prompt": "same prompt"},
    )
    replay = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-text-default"},
        json={"prompt": "same prompt", "generation_type": "TEXT_TO_IMAGE"},
    )
    conflict = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-text-default"},
        json={"prompt": "different prompt", "generation_type": "TEXT_TO_IMAGE"},
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["generation_type"] == "TEXT_TO_IMAGE"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_unsupported_image_to_image_and_text_references_are_rejected(
    client: TestClient,
) -> None:
    unsupported = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-image-to-image"},
        json={"prompt": "edit this", "generation_type": "IMAGE_TO_IMAGE"},
    )
    reference = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-text-reference"},
        json={"prompt": "draw this", "reference_asset_id": str(uuid4())},
    )

    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "GENERATION_TYPE_UNSUPPORTED"
    assert reference.status_code == 422
    assert reference.json()["error"]["code"] == "REFERENCE_ASSET_NOT_ALLOWED"


def test_unavailable_selected_provider_profile_returns_stable_configuration_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "dashscope")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_HOST", raising=False)

    response = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-provider-unavailable"},
        json={"prompt": "do not route to another provider"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROVIDER_PROFILE_UNAVAILABLE"


def test_same_key_replay_returns_200_and_conflict_returns_stable_409(client: TestClient) -> None:
    first = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-2"},
        json={"prompt": "same prompt"},
    )
    replay = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-2"},
        json={"prompt": "same prompt"},
    )
    conflict = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-2"},
        json={"prompt": "different prompt"},
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["idempotency_replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert conflict.json()["error"]["request_id"]


def test_create_validation_and_missing_key_use_stable_errors(client: TestClient) -> None:
    missing_key = client.post("/api/v1/tasks", json={"prompt": "valid"})
    invalid_prompt = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-3"},
        json={"prompt": "   "},
    )

    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert invalid_prompt.status_code == 422
    assert invalid_prompt.json()["error"]["code"] == "INVALID_PROMPT"


def test_demo_route_is_opt_in_and_keeps_execution_profile_internal() -> None:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for API tests")
    session_factory = create_session_factory(database_url)
    hidden = TestClient(create_app(session_factory=session_factory, demo_mode=False))
    enabled = TestClient(create_app(session_factory=session_factory, demo_mode=True))

    assert hidden.post(
        "/api/v1/demo/tasks",
        headers={"Idempotency-Key": f"hidden-{uuid4()}"},
        json={"prompt": "hidden", "scenario": "success"},
    ).status_code == 404

    response = enabled.post(
        "/api/v1/demo/tasks",
        headers={"Idempotency-Key": f"demo-{uuid4()}"},
        json={"prompt": "temporary recovery", "scenario": "transient_then_success"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "QUEUED"
    assert "execution_profile" not in body
    with session_factory() as session:
        task = session.get(GenerationTaskModel, body["id"])
    assert task is not None
    assert task.execution_profile == "transient_then_success"
    assert task.provider_profile == "mock-text-to-image-v1"


def test_openapi_exposes_generated_status_and_error_contract(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["components"]["schemas"]["TaskStatus"]["enum"] == [
        "QUEUED",
        "RUNNING",
        "RETRY_WAIT",
        "SUCCEEDED",
        "FAILED",
    ]
    assert "ErrorResponse" in schema["components"]["schemas"]
    assert schema["components"]["schemas"]["GenerationType"]["enum"] == [
        "TEXT_TO_IMAGE",
        "IMAGE_TO_IMAGE",
    ]


def test_detail_history_and_not_found_are_available(client: TestClient) -> None:
    created = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-4"},
        json={"prompt": "history prompt"},
    )
    task_id = created.json()["id"]

    detail = client.get(f"/api/v1/tasks/{task_id}")
    history = client.get("/api/v1/tasks", params={"limit": 1, "status": "QUEUED"})
    missing = client.get(f"/api/v1/tasks/{uuid4()}")

    assert detail.status_code == 200
    assert detail.json()["events"][0]["type"] == "TASK_QUEUED"
    assert detail.json()["generation_type"] == "TEXT_TO_IMAGE"
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == task_id
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_liveness_and_readiness(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    ready = client.get("/api/v1/health/ready")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_historical_result_asset_still_downloads_from_its_saved_object_key() -> None:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for API tests")
    session_factory = create_session_factory(database_url)
    task_id = CreateTask(session_factory).execute(
        CreateTaskRequest(prompt="historical result download"), f"history-download-{uuid4()}"
    ).task.id

    class RecordingAssetStore:
        def __init__(self) -> None:
            self.download_keys: list[str] = []

        def put_result(self, **_: object) -> StoredAsset:
            raise AssertionError("the historical download route must not write an object")

        def presigned_download(
            self, object_key: str, *, expires_seconds: int = 300
        ) -> str:
            self.download_keys.append(object_key)
            return f"https://objects.example/{object_key}?expires={expires_seconds}"

        def check_ready(self) -> None:
            return None

    asset_store = RecordingAssetStore()
    legacy_key: str | None = None
    try:
        assert ExecuteGenerationAttempt(session_factory, MockProvider()).execute(task_id).succeeded
        with session_factory.begin() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
            )
            assert attempt is not None
            legacy_key = f"results/{task_id}/{attempt.id}/0.png"
            session.add(
                ResultAssetModel(
                    id=uuid4(),
                    task_id=task_id,
                    attempt_id=attempt.id,
                    role="RESULT",
                    object_key=legacy_key,
                    content_type="image/png",
                    size_bytes=42,
                    sha256="a" * 64,
                    created_at=attempt.started_at,
                )
            )

        client = TestClient(create_app(session_factory=session_factory, asset_store=asset_store))
        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["result"]["width"] is None
        assert detail.json()["result"]["height"] is None
        download_url = detail.json()["result"]["download_url"]
        assert download_url.endswith(f"/api/v1/assets/{detail.json()['result']['id']}/download")

        download = client.get(download_url, follow_redirects=False)
        assert download.status_code == 307
        assert legacy_key is not None
        assert asset_store.download_keys == [legacy_key]
        assert legacy_key in download.headers["location"]
    finally:
        with session_factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
            )
            session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
            task = session.get(GenerationTaskModel, task_id)
            if task is not None:
                session.delete(task)


def test_manual_retry_copies_provider_and_policy_snapshots() -> None:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for API tests")
    session_factory = create_session_factory(database_url)
    original = CreateTask(session_factory).execute(
        CreateTaskRequest(prompt="preserve retry snapshots"), f"retry-source-{uuid4()}"
    )
    source_id = original.task.id
    child_id = None
    try:
        with session_factory.begin() as session:
            task = session.get(GenerationTaskModel, source_id)
            assert task is not None
            task.status = "FAILED"
            task.error_code = "PROVIDER_UNAVAILABLE"

        client = TestClient(create_app(session_factory=session_factory))
        response = client.post(
            f"/api/v1/tasks/{source_id}/retry",
            headers={"Idempotency-Key": f"retry-child-{uuid4()}"},
        )
        assert response.status_code == 201
        child_id = UUID(response.json()["id"])
        with session_factory() as session:
            source = session.get(GenerationTaskModel, source_id)
            child = session.get(GenerationTaskModel, child_id)
        assert source is not None and child is not None
        assert child.provider_profile == source.provider_profile
        assert child.provider_name == source.provider_name
        assert child.model_name == source.model_name
        assert child.capability_version == source.capability_version
        assert child.policy_version == source.policy_version
        assert child.max_attempts == source.max_attempts
        assert child.policy_snapshot == source.policy_snapshot
    finally:
        with session_factory.begin() as session:
            for task_id in (child_id, source_id):
                if task_id is None:
                    continue
                session.execute(
                    delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
                )
                session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
                task = session.get(GenerationTaskModel, task_id)
                if task is not None:
                    session.delete(task)


def test_new_result_persists_actual_dimensions_from_image_bytes() -> None:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for API tests")
    session_factory = create_session_factory(database_url)

    class RecordingAssetStore:
        def put_result(self, *, task_id, attempt_id, content: bytes, content_type: str):
            identity = result_identity(content, content_type)
            return StoredAsset(
                object_key=candidate_object_key(task_id, attempt_id, identity),
                content_type=identity.content_type,
                size_bytes=identity.size_bytes,
                sha256=identity.sha256,
                width=identity.width,
                height=identity.height,
            )

        def presigned_download(self, object_key: str, *, expires_seconds: int = 300) -> str:
            return f"https://objects.example/{object_key}?expires={expires_seconds}"

        def check_ready(self) -> None:
            return None

    store = RecordingAssetStore()
    task_id = CreateTask(session_factory).execute(
        CreateTaskRequest(prompt="result with actual dimensions"), f"dimension-{uuid4()}"
    ).task.id
    try:
        outcome = ExecuteGenerationAttempt(
            session_factory, MockProvider(), asset_store=store
        ).execute(task_id)
        assert outcome.succeeded is True
        with session_factory() as session:
            asset = session.scalar(
                select(ResultAssetModel).where(
                    ResultAssetModel.task_id == task_id, ResultAssetModel.role == "RESULT"
                )
            )
        assert asset is not None
        assert (asset.width, asset.height) == (1280, 1280)

        client = TestClient(create_app(session_factory=session_factory, asset_store=store))
        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["result"]["width"] == 1280
        assert detail.json()["result"]["height"] == 1280
    finally:
        with session_factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
            )
            session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
            task = session.get(GenerationTaskModel, task_id)
            if task is not None:
                session.delete(task)
