from __future__ import annotations

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select

from museflow.api.app import create_app
from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ReferenceAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.reference_assets.image_inspector import ImageInspector
from museflow.reference_assets.object_keys import reference_object_key
from museflow.reference_assets.service import reference_request_fingerprint
from museflow.tasks.domain import ReferenceAssetStatus
from museflow.tasks.execution_models import GenerationAttemptModel


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.put_keys: list[str] = []
        self.lock = threading.Lock()
        self.fail_put = False

    def put(self, object_key, stream, *, size_bytes, content_type, sha256) -> None:
        if self.fail_put:
            from museflow.reference_assets.blob_store import BlobStoreUnavailable

            raise BlobStoreUnavailable("injected failure")
        content = stream.read()
        assert len(content) == size_bytes
        assert hashlib.sha256(content).hexdigest() == sha256
        with self.lock:
            self.objects[object_key] = (content, content_type, sha256)
            self.put_keys.append(object_key)

    def get(self, object_key: str, *, max_bytes: int = 6_000_000) -> bytes:
        from museflow.reference_assets.blob_store import BlobNotFoundError

        try:
            content = self.objects[object_key][0]
            if len(content) > max_bytes:
                raise ValueError("too large")
            return content
        except KeyError as error:
            raise BlobNotFoundError("missing") from error

    def stat(self, object_key: str):
        from museflow.reference_assets.blob_store import BlobMetadata, BlobNotFoundError

        try:
            content, content_type, sha256 = self.objects[object_key]
        except KeyError as error:
            raise BlobNotFoundError("missing") from error
        return BlobMetadata(len(content), content_type, sha256)

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)

    def presigned_get(self, object_key: str, *, expires_seconds: int = 300) -> str:
        return f"https://private.invalid/{object_key}?expires={expires_seconds}"


def _png(color: tuple[int, int, int] = (220, 20, 60)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 512), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture()
def api() -> tuple[TestClient, object, InMemoryBlobStore]:
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for reference asset API tests")
    factory = create_session_factory(database_url)
    blob_store = InMemoryBlobStore()
    client = TestClient(create_app(session_factory=factory, reference_blob_store=blob_store))
    yield client, factory, blob_store


def _cleanup(factory: object, blob_store: InMemoryBlobStore, asset_ids: list[UUID]) -> None:
    for asset_id in asset_ids:
        with factory() as session:  # type: ignore[operator]
            asset = session.get(ReferenceAssetModel, asset_id)
            object_key = asset.object_key if asset is not None else None
        if object_key:
            blob_store.delete(object_key)
    with factory.begin() as session:  # type: ignore[operator]
        for asset_id in asset_ids:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == asset_id)
            )
            session.execute(delete(ReferenceAssetModel).where(ReferenceAssetModel.id == asset_id))


def test_upload_is_idempotent_and_download_uses_verified_stable_path(api) -> None:
    client, factory, blob_store = api
    png = _png()
    asset_ids: list[UUID] = []
    try:
        missing_key = client.post("/api/v1/assets", files={"file": ("input.png", png, "image/png")})
        first = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-upload-one"},
            files={"file": ("first.png", png, "image/png")},
        )
        replay = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-upload-one"},
            files={"file": ("renamed.jpg", png, "image/png")},
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["idempotency_replayed"] is True
        assert replay.json()["asset_id"] == first.json()["asset_id"]
        asset_id = UUID(first.json()["asset_id"])
        asset_ids.append(asset_id)
        assert first.json()["status"] == "READY"
        assert first.json()["content_type"] == "image/png"
        assert first.json()["download_url"] == f"/api/v1/assets/{asset_id}/download"
        assert not any(key in first.json() for key in ("object_key", "filename", "url"))
        assert len(blob_store.objects) == 1
        assert len(blob_store.put_keys) == 1

        metadata = client.get(f"/api/v1/assets/{asset_id}")
        downloaded = client.get(f"/api/v1/assets/{asset_id}/download")
        signed_access = client.get(f"/api/v1/assets/{asset_id}/access", follow_redirects=False)
        assert metadata.status_code == 200
        assert metadata.json()["sha256"] == hashlib.sha256(png).hexdigest()
        assert downloaded.status_code == 200
        assert downloaded.content == png
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert signed_access.status_code == 307
        assert signed_access.headers["location"].endswith("?expires=300")
        assert client.get("/api/v1/assets/missing").status_code == 422
    finally:
        _cleanup(factory, blob_store, asset_ids)


def test_uploaded_ready_asset_creates_idempotent_image_to_image_task_and_detail(api, monkeypatch):
    client, factory, blob_store = api
    monkeypatch.setenv("MUSEFLOW_PROVIDER", "mock")
    content = _png((15, 90, 160))
    asset_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        uploaded = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": f"i2i-upload-{uuid4()}"},
            files={"file": ("input.png", content, "image/png")},
        )
        assert uploaded.status_code == 201
        asset_id = UUID(uploaded.json()["asset_id"])
        asset_ids.append(asset_id)
        assert uploaded.json()["status"] == "READY"

        task_request = {
            "generation_type": "IMAGE_TO_IMAGE",
            "prompt": "turn this into a blue-hour illustration",
            "size_preset": "1280*1280",
            "reference_asset_id": str(asset_id),
        }
        key = f"i2i-task-{uuid4()}"
        created = client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json=task_request)
        replay = client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json=task_request)
        assert created.status_code == 201
        assert created.json()["generation_type"] == "IMAGE_TO_IMAGE"
        assert created.json()["idempotency_replayed"] is False
        assert replay.status_code == 200
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["idempotency_replayed"] is True
        task_id = UUID(created.json()["id"])
        task_ids.append(task_id)

        with factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            assert task is not None
            assert task.reference_asset_id == asset_id
            assert task.reference_sha256 == hashlib.sha256(content).hexdigest()
            assert task.provider_profile == "mock-image-generation-v2"
            assert task.model_name == "mock-deterministic-image"
            assert task.capability_version == "image-generation-v2"
            assert (
                session.scalar(
                    select(OutboxMessageModel.id).where(OutboxMessageModel.aggregate_id == task_id)
                )
                is not None
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(TaskEventModel).where(TaskEventModel.task_id == task_id)
                        )
                    )
                )
                == 1
            )

        detail = client.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["input_summary"] == {
            "generation_type": "IMAGE_TO_IMAGE",
            "prompt": task_request["prompt"],
            "size_preset": "1280*1280",
            "reference_asset_id": str(asset_id),
            "reference_sha256": hashlib.sha256(content).hexdigest(),
        }
        assert detail_body["reference_download_url"] == f"/api/v1/assets/{asset_id}/download"
        assert detail_body["provider_profile"] == "mock-image-generation-v2"
        assert "object_key" not in detail_body
        assert "remote_request_id" not in detail_body

        history = client.get(
            "/api/v1/tasks",
            params={"generation_type": "IMAGE_TO_IMAGE", "status": "QUEUED"},
        )
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [str(task_id)]

        different_asset = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": f"i2i-upload-{uuid4()}"},
            files={"file": ("same-content.png", content, "image/png")},
        )
        assert different_asset.status_code == 201
        different_id = UUID(different_asset.json()["asset_id"])
        asset_ids.append(different_id)
        conflict = client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": key},
            json={**task_request, "reference_asset_id": str(different_id)},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    finally:
        with factory.begin() as session:
            for task_id in task_ids:
                session.execute(
                    delete(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
                )
                session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
                session.execute(
                    delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
                )
                session.execute(
                    delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id)
                )
        _cleanup(factory, blob_store, asset_ids)


def test_same_key_different_image_returns_stable_conflict(api) -> None:
    client, factory, blob_store = api
    asset_ids: list[UUID] = []
    try:
        first = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-upload-conflict"},
            files={"file": ("one.png", _png(), "image/png")},
        )
        asset_ids.append(UUID(first.json()["asset_id"]))
        conflict = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-upload-conflict"},
            files={"file": ("two.png", _png((10, 20, 30)), "image/png")},
        )

        assert first.status_code == 201
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
        assert len(blob_store.objects) == 1
    finally:
        _cleanup(factory, blob_store, asset_ids)


def test_upload_rejects_declared_type_mismatch_and_stream_limit(api) -> None:
    client, factory, blob_store = api
    asset_ids: list[UUID] = []
    try:
        mismatch = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-mime-mismatch"},
            files={"file": ("actual.png", _png(), "image/jpeg")},
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["error"]["code"] == "IMAGE_CONTENT_TYPE_MISMATCH"

        oversized = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-file-too-large"},
            files={
                "file": (
                    "oversized.png",
                    _png() + b"0" * (6_000_001 - len(_png())),
                    "image/png",
                )
            },
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "FILE_TOO_LARGE"
        assert blob_store.objects == {}
    finally:
        _cleanup(factory, blob_store, asset_ids)


def test_object_store_failure_leaves_recoverable_staging_and_same_key_replay(api) -> None:
    client, factory, blob_store = api
    asset_ids: list[UUID] = []
    try:
        blob_store.fail_put = True
        failed = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-minio-failure"},
            files={"file": ("input.png", _png(), "image/png")},
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "OBJECT_STORE_UNAVAILABLE"
        with factory() as session:  # type: ignore[operator]
            record = session.scalar(
                select(ReferenceAssetModel).where(
                    ReferenceAssetModel.idempotency_key == "reference-minio-failure"
                )
            )
            assert record is not None and record.status == "STAGING"
            asset_ids.append(record.id)

        blob_store.fail_put = False
        replay = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": "reference-minio-failure"},
            files={"file": ("input.png", _png(), "image/png")},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotency_replayed"] is True
        assert replay.json()["status"] == "READY"
    finally:
        _cleanup(factory, blob_store, asset_ids)


def test_concurrent_same_key_uploads_reserve_one_asset_and_one_object(api) -> None:
    client, factory, blob_store = api
    app = client.app
    png = _png()
    key = f"reference-concurrent-{UUID(int=1)}"
    barrier = threading.Barrier(2)

    def upload() -> int:
        barrier.wait(timeout=5)
        with TestClient(app) as concurrent_client:
            return concurrent_client.post(
                "/api/v1/assets",
                headers={"Idempotency-Key": key},
                files={"file": ("input.png", png, "image/png")},
            ).status_code

    asset_ids: list[UUID] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: upload(), range(2)))
        assert 201 in statuses
        assert all(status in {200, 201, 409} for status in statuses)
        with factory() as session:  # type: ignore[operator]
            records = list(
                session.scalars(
                    select(ReferenceAssetModel).where(ReferenceAssetModel.idempotency_key == key)
                )
            )
        assert len(records) == 1
        asset_ids.append(records[0].id)
        assert len(blob_store.objects) == 1
        assert len(blob_store.put_keys) == 1
    finally:
        _cleanup(factory, blob_store, asset_ids)


def test_concurrent_same_key_different_images_conflict_without_second_object(api) -> None:
    client, factory, blob_store = api
    app = client.app
    key = f"reference-concurrent-conflict-{uuid4()}"
    barrier = threading.Barrier(2)
    contents = [_png(), _png((10, 20, 30))]

    def upload(content: bytes) -> tuple[int, str | None]:
        barrier.wait(timeout=5)
        with TestClient(app) as concurrent_client:
            response = concurrent_client.post(
                "/api/v1/assets",
                headers={"Idempotency-Key": key},
                files={"file": ("input.png", content, "image/png")},
            )
            return response.status_code, response.json().get("asset_id")

    asset_ids: list[UUID] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(upload, contents))
        assert sorted(status for status, _ in results) == [201, 409]
        with factory() as session:  # type: ignore[operator]
            records = list(
                session.scalars(
                    select(ReferenceAssetModel).where(ReferenceAssetModel.idempotency_key == key)
                )
            )
        assert len(records) == 1
        asset_ids.append(records[0].id)
        assert len(blob_store.objects) == 1
        assert len(blob_store.put_keys) == 1
    finally:
        _cleanup(factory, blob_store, asset_ids)


@pytest.mark.parametrize(
    "status",
    [
        ReferenceAssetStatus.FAILED,
        ReferenceAssetStatus.DELETE_PENDING,
        ReferenceAssetStatus.DELETED,
    ],
)
def test_replay_does_not_revive_non_reusable_asset_states(
    api, status: ReferenceAssetStatus
) -> None:
    client, factory, blob_store = api
    content = _png()
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(prefix="museflow-idempotency-test-", delete=False) as file:
        metadata_path = Path(file.name)
        file.write(content)
    try:
        metadata = ImageInspector().inspect(metadata_path, declared_content_type="image/png")
    finally:
        metadata_path.unlink(missing_ok=True)
    fingerprint = reference_request_fingerprint(
        sha256=metadata.sha256,
        content_type=metadata.content_type,
        width=metadata.width,
        height=metadata.height,
        size_bytes=metadata.size_bytes,
        frame_count=metadata.frame_count,
        color_mode=metadata.color_mode,
    )
    asset_id = uuid4()
    key = reference_object_key(asset_id, metadata.sha256, metadata.content_type)
    idempotency_key = f"reference-state-{status.value}-{uuid4()}"
    with factory.begin() as session:  # type: ignore[operator]
        session.add(
            ReferenceAssetModel(
                id=asset_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status=status.value,
                object_key=key,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
                width=metadata.width,
                height=metadata.height,
                sha256=metadata.sha256,
                created_at=datetime.now(UTC),
            )
        )
    try:
        response = client.post(
            "/api/v1/assets",
            headers={"Idempotency-Key": idempotency_key},
            files={"file": ("retry.png", content, "image/png")},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REFERENCE_ASSET_NOT_REUSABLE"
        assert blob_store.put_keys == []
        assert client.get(f"/api/v1/assets/{asset_id}/download").status_code == 409
    finally:
        _cleanup(factory, blob_store, [asset_id])


def test_small_raw_body_guard_rejects_before_multipart_or_storage(api) -> None:
    _client, factory, blob_store = api
    client = TestClient(
        create_app(session_factory=factory, reference_blob_store=blob_store, raw_body_limit=512)
    )
    before = len(blob_store.put_keys)
    response = client.post(
        "/api/v1/assets",
        headers={"Idempotency-Key": "raw-limit"},
        files={"file": ("input.png", _png(), "image/png")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "MULTIPART_BODY_TOO_LARGE"
    assert len(blob_store.put_keys) == before
