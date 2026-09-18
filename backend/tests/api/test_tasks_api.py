from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from museflow.api.app import create_app
from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.db.session import create_session_factory


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
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == task_id
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_liveness_and_readiness(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    ready = client.get("/api/v1/health/ready")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
