from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError

from museflow.api.app import create_app
from museflow.db.session import create_session_factory


@pytest.fixture()
def migration_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[URL, Engine]]:
    base_value = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not base_value:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL migration tests")

    base_url = make_url(base_value)
    database_name = f"museflow_migration_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    database_url = base_url.set(database=database_name)
    engine = sa.create_engine(database_url)
    monkeypatch.setenv("MUSEFLOW_DATABASE_URL", database_url.render_as_string(hide_password=False))
    try:
        yield database_url, engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def _alembic_config(database_url: URL) -> Config:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _upgrade(database_url: URL, revision: str) -> None:
    command.upgrade(_alembic_config(database_url), revision)


def _insert_task(
    connection: sa.Connection,
    *,
    task_id: UUID | None = None,
    generation_type: str = "TEXT_TO_IMAGE",
    reference_asset_id: UUID | None = None,
    reference_sha256: str | None = None,
    provider_snapshot: bool = True,
    retried_from_task_id: UUID | None = None,
) -> UUID:
    task_id = task_id or uuid4()
    fields = [
        "id", "idempotency_key", "request_fingerprint", "prompt", "size_preset", "status",
        "max_attempts", "policy_version", "deadline_at", "created_at", "queued_at", "version",
        "retried_from_task_id", "generation_type", "reference_asset_id", "reference_sha256",
        "policy_snapshot", "provider_profile", "provider_name", "model_name",
        "capability_version",
    ]
    values: dict[str, object] = {
        "id": task_id,
        "idempotency_key": f"task-{uuid4()}",
        "request_fingerprint": "a" * 64,
        "prompt": "historical request",
        "size_preset": "1280*1280",
        "status": "QUEUED",
        "max_attempts": 3,
        "policy_version": "mvp-0.2",
        "deadline_at": datetime.now(UTC) + timedelta(minutes=10),
        "created_at": datetime.now(UTC),
        "queued_at": datetime.now(UTC),
        "version": 1,
        "retried_from_task_id": retried_from_task_id,
        "generation_type": generation_type,
        "reference_asset_id": reference_asset_id,
        "reference_sha256": reference_sha256,
        "policy_snapshot": json.dumps({"version": "test-v1"}),
        "provider_profile": "mock-text-to-image-v1",
        "provider_name": "mock",
        "model_name": "mock-text-to-image",
        "capability_version": "text-to-image-v1",
    }
    if not provider_snapshot:
        fields = [
            field for field in fields
            if field
            not in {"provider_profile", "provider_name", "model_name", "capability_version"}
        ]
        values.pop("provider_profile")
        values.pop("provider_name")
        values.pop("model_name")
        values.pop("capability_version")
    named_fields = ", ".join(fields)
    named_values = ", ".join(
        "CAST(:policy_snapshot AS jsonb)" if field == "policy_snapshot" else f":{field}"
        for field in fields
    )
    connection.execute(
        text(f"INSERT INTO generation_tasks ({named_fields}) VALUES ({named_values})"), values
    )
    return task_id


def _insert_reference(
    connection: sa.Connection,
    *,
    asset_id: UUID | None = None,
    idempotency_key: str | None = None,
    object_key: str | None = None,
    status: str = "READY",
) -> UUID:
    asset_id = asset_id or uuid4()
    connection.execute(
        text(
            "INSERT INTO reference_assets "
            "(id, idempotency_key, request_fingerprint, status, object_key, content_type, "
            "size_bytes, width, height, sha256, created_at, ready_at) "
            "VALUES (:id, :key, :fingerprint, :status, :object_key, :content_type, "
            ":size_bytes, :width, :height, :sha256, :created_at, :ready_at)"
        ),
        {
            "id": asset_id,
            "key": idempotency_key or f"reference-{uuid4()}",
            "fingerprint": "b" * 64,
            "status": status,
            "object_key": object_key or f"references/{asset_id}/original.png",
            "content_type": "image/png",
            "size_bytes": 13,
            "width": 1,
            "height": 1,
            "sha256": "c" * 64,
            "created_at": datetime.now(UTC),
            "ready_at": datetime.now(UTC) if status == "READY" else None,
        },
    )
    return asset_id


def _assert_constraint_rejects(
    engine: Engine, statement: str, parameters: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(statement), parameters)


def test_empty_database_upgrades_to_head_repeatedly_without_runtime_services(
    migration_database: tuple[URL, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, engine = migration_database
    for key in (
        "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET",
        "REDIS_URL", "MUSEFLOW_PROVIDER", "DASHSCOPE_API_KEY", "DASHSCOPE_API_HOST",
    ):
        monkeypatch.delenv(key, raising=False)

    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0006_compatibility_constraints"
        assert connection.execute(text("SELECT count(*) FROM reference_assets")).scalar_one() == 0

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reference_assets")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM generation_tasks")).scalar_one() == 0


def test_stage_two_history_is_preserved_and_backfilled_deterministically(
    migration_database: tuple[URL, Engine]
) -> None:
    database_url, engine = migration_database
    _upgrade(database_url, "0004_demo_execution_profiles")
    task_id = UUID("10000000-0000-0000-0000-000000000001")
    retried_task_id = UUID("10000000-0000-0000-0000-000000000002")
    attempt_id = UUID("20000000-0000-0000-0000-000000000001")
    created_at = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    object_key = f"results/{task_id}/{attempt_id}/0.png"
    with engine.begin() as connection:
        for current_id, status in ((task_id, "SUCCEEDED"), (retried_task_id, "FAILED")):
            connection.execute(
                text(
                    "INSERT INTO generation_tasks (id, idempotency_key, request_fingerprint, "
                    "prompt, size_preset, status, max_attempts, policy_version, deadline_at, "
                    "created_at, queued_at, version, retried_from_task_id, execution_profile) "
                    "VALUES (:id, :key, :fingerprint, :prompt, '1280*1280', :status, 3, "
                    "'mvp-0.2', :deadline, :created_at, :created_at, 1, :retried_from, NULL)"
                ),
                {
                    "id": current_id,
                    "key": f"historical-{current_id}",
                    "fingerprint": "d" * 64,
                    "prompt": "preserve historical task",
                    "status": status,
                    "deadline": created_at + timedelta(minutes=10),
                    "created_at": created_at,
                    "retried_from": task_id if current_id == retried_task_id else None,
                },
            )
        connection.execute(
            text(
                "INSERT INTO generation_attempts (id, task_id, sequence, status, phase, "
                "provider_name, provider_request_key, provider_request_id, execution_token, "
                "lease_expires_at, started_at, finished_at, result_digest, result_metadata) "
                "VALUES (:id, :task_id, 1, 'SUCCEEDED', 'COMPLETED', 'dashscope', 'local-key', "
                "'remote-id', :token, :lease, :started, :finished, :digest, '{}'::jsonb)"
            ),
            {
                "id": attempt_id,
                "task_id": task_id,
                "token": UUID("30000000-0000-0000-0000-000000000001"),
                "lease": created_at + timedelta(minutes=1),
                "started": created_at,
                "finished": created_at + timedelta(seconds=4),
                "digest": "e" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO result_assets (id, task_id, attempt_id, role, object_key, "
                "content_type, size_bytes, sha256, created_at) VALUES (:id, :task_id, :attempt_id, "
                "'RESULT', :object_key, 'image/png', 42, :sha, :created_at)"
            ),
            {
                "id": UUID("40000000-0000-0000-0000-000000000001"),
                "task_id": task_id,
                "attempt_id": attempt_id,
                "object_key": object_key,
                "sha": "f" * 64,
                "created_at": created_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO task_events (id, task_id, event_type, payload, created_at) "
                "VALUES (:id, :task_id, 'TASK_SUCCEEDED', '{}'::jsonb, :created_at)"
            ),
            {
                "id": UUID("50000000-0000-0000-0000-000000000001"),
                "task_id": task_id,
                "created_at": created_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO outbox_messages (id, message_type, aggregate_id, payload, "
                "available_at, published_at, created_at) VALUES (:id, 'EXECUTE_TASK', :task_id, "
                "jsonb_build_object('task_id', CAST(:task_id_text AS text)), "
                ":created_at, :created_at, :created_at)"
            ),
            {
                "id": UUID("60000000-0000-0000-0000-000000000001"),
                "task_id": task_id,
                "task_id_text": str(task_id),
                "created_at": created_at,
            },
        )

    _upgrade(database_url, "head")
    _upgrade(database_url, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM generation_tasks")).scalar_one() == 2
        assert (
            connection.execute(text("SELECT count(*) FROM generation_attempts")).scalar_one()
            == 1
        )
        assert connection.execute(text("SELECT count(*) FROM task_events")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM outbox_messages")).scalar_one() == 1
        task = connection.execute(
            text(
                "SELECT generation_type, reference_asset_id, reference_sha256, provider_profile, "
                "provider_name, model_name, capability_version, policy_snapshot "
                "FROM generation_tasks WHERE id = :task_id"
            ),
            {"task_id": task_id},
        ).mappings().one()
        assert task["generation_type"] == "TEXT_TO_IMAGE"
        assert task["reference_asset_id"] is None
        assert task["reference_sha256"] is None
        assert task["provider_profile"] == "legacy-unfrozen-v1"
        assert task["provider_name"] == "legacy-unknown"
        assert task["model_name"] == "legacy-unknown"
        assert task["capability_version"] == "legacy-unknown"
        assert task["policy_snapshot"] == {"version": "legacy-unfrozen-v1", "frozen": False}
        result = connection.execute(
            text(
                "SELECT object_key, attempt_id, width, height FROM result_assets "
                "WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        ).mappings().one()
        assert result["object_key"] == object_key
        assert result["attempt_id"] == attempt_id
        assert result["width"] is None and result["height"] is None
        assert connection.execute(
            text("SELECT task_id FROM task_events WHERE id = :event_id"),
            {"event_id": UUID("50000000-0000-0000-0000-000000000001")},
        ).scalar_one() == task_id
        assert connection.execute(
            text("SELECT aggregate_id FROM outbox_messages WHERE id = :outbox_id"),
            {"outbox_id": UUID("60000000-0000-0000-0000-000000000001")},
        ).scalar_one() == task_id
        assert connection.execute(
            text("SELECT retried_from_task_id FROM generation_tasks WHERE id = :task_id"),
            {"task_id": retried_task_id},
        ).scalar_one() == task_id
        assert connection.execute(
            text("SELECT provider_request_id FROM generation_attempts WHERE id = :attempt_id"),
            {"attempt_id": attempt_id},
        ).scalar_one() == "remote-id"

    class RecordingAssetStore:
        def __init__(self) -> None:
            self.download_keys: list[str] = []

        def put_result(self, **_: object) -> object:
            raise AssertionError("migration compatibility checks do not write assets")

        def presigned_download(self, key: str, *, expires_seconds: int = 300) -> str:
            self.download_keys.append(key)
            return f"https://objects.example/{key}?expires={expires_seconds}"

        def check_ready(self) -> None:
            return None

    asset_store = RecordingAssetStore()
    client = TestClient(
        create_app(
            session_factory=create_session_factory(
                database_url.render_as_string(hide_password=False)
            ),
            asset_store=asset_store,  # type: ignore[arg-type]
        )
    )
    detail = client.get(f"/api/v1/tasks/{task_id}")
    first_page = client.get("/api/v1/tasks", params={"limit": 1})
    second_page = client.get(
        "/api/v1/tasks", params={"limit": 1, "cursor": first_page.json()["next_cursor"]}
    )
    download = client.get(
        detail.json()["result"]["download_url"], follow_redirects=False
    )
    assert detail.status_code == 200
    assert detail.json()["generation_type"] == "TEXT_TO_IMAGE"
    assert detail.json()["result"]["width"] is None
    assert detail.json()["result"]["height"] is None
    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()["items"][0]["id"] == str(retried_task_id)
    assert second_page.json()["items"][0]["id"] == str(task_id)
    assert download.status_code == 307
    assert asset_store.download_keys == [object_key]


def test_generation_and_reference_constraints_preserve_asset_lifecycles(
    migration_database: tuple[URL, Engine]
) -> None:
    database_url, engine = migration_database
    _upgrade(database_url, "head")
    reference_id = uuid4()
    with engine.begin() as connection:
        _insert_reference(connection, asset_id=reference_id)
        _insert_task(connection, generation_type="IMAGE_TO_IMAGE", reference_asset_id=reference_id,
                     reference_sha256="c" * 64)
        _insert_task(connection)

    invalid_values = (
        ("TEXT_TO_IMAGE", reference_id, None),
        ("TEXT_TO_IMAGE", None, "c" * 64),
        ("IMAGE_TO_IMAGE", None, "c" * 64),
        ("IMAGE_TO_IMAGE", reference_id, None),
        ("VIDEO_TO_IMAGE", None, None),
    )
    for generation_type, reference_asset_id, reference_sha256 in invalid_values:
        _assert_constraint_rejects(
            engine,
            "INSERT INTO generation_tasks "
            "(id, idempotency_key, request_fingerprint, prompt, size_preset, status, "
            "max_attempts, policy_version, deadline_at, created_at, queued_at, version, "
            "generation_type, reference_asset_id, reference_sha256, policy_snapshot, "
            "provider_profile, provider_name, model_name, capability_version) "
            "VALUES (:id, :key, :fingerprint, 'bad', '1280*1280', 'QUEUED', 3, 'mvp-0.2', "
            "now() + interval '10 minutes', now(), now(), 1, :generation_type, :reference_id, "
            ":reference_sha, '{}'::jsonb, 'profile', 'provider', 'model', 'v1')",
            {
                "id": uuid4(),
                "key": f"invalid-{uuid4()}",
                "fingerprint": "a" * 64,
                "generation_type": generation_type,
                "reference_id": reference_asset_id,
                "reference_sha": reference_sha256,
            },
        )

    _assert_constraint_rejects(
        engine,
        "INSERT INTO generation_tasks "
        "(id, idempotency_key, request_fingerprint, prompt, size_preset, status, max_attempts, "
        "policy_version, deadline_at, created_at, queued_at, version, generation_type, "
        "reference_asset_id, reference_sha256, policy_snapshot, provider_profile, provider_name, "
        "model_name, capability_version) VALUES (:id, :key, :fingerprint, 'bad', '1280*1280', "
        "'QUEUED', 3, 'mvp-0.2', now() + interval '10 minutes', now(), now(), 1, "
        "'IMAGE_TO_IMAGE', :reference_id, :sha, '{}'::jsonb, 'profile', 'provider', 'model', 'v1')",
        {
            "id": uuid4(),
            "key": f"missing-reference-{uuid4()}",
            "fingerprint": "a" * 64,
            "reference_id": uuid4(),
            "sha": "c" * 64,
        },
    )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM reference_assets WHERE id = :reference_id"),
                {"reference_id": reference_id},
            )

    for duplicate_field, duplicate_value in (
        ("idempotency_key", "unique-reference-key"),
        ("object_key", "references/shared/object.png"),
    ):
        with engine.begin() as connection:
            _insert_reference(
                connection,
                idempotency_key=(
                    duplicate_value
                    if duplicate_field == "idempotency_key"
                    else f"unique-key-{uuid4()}"
                ),
                object_key=(
                    duplicate_value
                    if duplicate_field == "object_key"
                    else f"references/{uuid4()}/object.png"
                ),
            )
        _assert_constraint_rejects(
            engine,
            "INSERT INTO reference_assets "
            "(id, idempotency_key, request_fingerprint, status, object_key, created_at) "
            "VALUES (:id, :idempotency_key, :fingerprint, 'STAGING', :object_key, now())",
            {
                "id": uuid4(),
                "idempotency_key": duplicate_value if duplicate_field == "idempotency_key"
                else f"different-key-{uuid4()}",
                "fingerprint": "b" * 64,
                "object_key": duplicate_value if duplicate_field == "object_key"
                else f"references/{uuid4()}/object.png",
            },
        )

    _assert_constraint_rejects(
        engine,
        "INSERT INTO reference_assets (id, idempotency_key, request_fingerprint, status, "
        "object_key, created_at) VALUES (:id, :key, :fingerprint, 'UPLOADING', :object_key, now())",
        {"id": uuid4(), "key": f"bad-status-{uuid4()}", "fingerprint": "b" * 64,
         "object_key": f"references/{uuid4()}/object.png"},
    )

    with engine.begin() as connection:
        task_id = _insert_task(connection)
        attempt_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO generation_attempts (id, task_id, sequence, status, phase, "
                "provider_name, provider_request_key, execution_token, lease_expires_at, "
                "started_at) "
                "VALUES (:id, :task_id, 1, 'RUNNING', 'PROVIDER_RUNNING', 'mock', 'key', :token, "
                "now() + interval '60 seconds', now())"
            ),
            {"id": attempt_id, "task_id": task_id, "token": uuid4()},
        )
        result_parameters = {
            "id": uuid4(), "task_id": task_id, "attempt_id": attempt_id,
            "object_key": f"results/{task_id}/one.png", "sha": "f" * 64,
        }
        connection.execute(
            text(
                "INSERT INTO result_assets (id, task_id, attempt_id, role, object_key, "
                "content_type, size_bytes, width, height, sha256, created_at) "
                "VALUES (:id, :task_id, :attempt_id, 'RESULT', :object_key, 'image/png', "
                "13, 1, 1, :sha, now())"
            ), result_parameters,
        )

    _assert_constraint_rejects(
        engine,
        "INSERT INTO result_assets (id, task_id, attempt_id, role, object_key, content_type, "
        "size_bytes, width, height, sha256, created_at) VALUES (:id, :task_id, :attempt_id, "
        "'RESULT', :object_key, 'image/png', 13, 1, 1, :sha, now())",
        {"id": uuid4(), "task_id": task_id, "attempt_id": attempt_id,
         "object_key": f"results/{task_id}/two.png", "sha": "f" * 64},
    )

    _assert_constraint_rejects(
        engine,
        "INSERT INTO generation_tasks "
        "(id, idempotency_key, request_fingerprint, prompt, size_preset, status, max_attempts, "
        "policy_version, deadline_at, created_at, queued_at, version, generation_type, "
        "policy_snapshot) VALUES (:id, :key, :fingerprint, 'missing provider', '1280*1280', "
        "'QUEUED', 3, 'mvp-0.2', now() + interval '10 minutes', now(), now(), 1, "
        "'TEXT_TO_IMAGE', '{}'::jsonb)",
        {"id": uuid4(), "key": f"missing-provider-{uuid4()}", "fingerprint": "a" * 64},
    )
