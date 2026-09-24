from __future__ import annotations

import hashlib
import io
import os
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from museflow.api.app import create_app
from museflow.db.models import OutboxMessageModel, ReferenceAssetModel
from museflow.db.session import create_session_factory
from museflow.reference_assets.blob_store import MinioBlobStore


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 512), (34, 90, 111)).save(output, format="PNG")
    return output.getvalue()


def test_reference_asset_upload_is_private_and_verified_through_api() -> None:
    if os.environ.get("MUSEFLOW_RUN_REFERENCE_MINIO_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REFERENCE_MINIO_TEST=1 with isolated Compose services")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")
    factory: sessionmaker[Session] = create_session_factory(database_url)
    blob_store = MinioBlobStore()
    blob_store.check_ready()
    client = TestClient(create_app(session_factory=factory, reference_blob_store=blob_store))
    content = _png()
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/api/v1/assets",
        headers={"Idempotency-Key": f"real-minio-{digest}"},
        files={"file": ("private.png", content, "image/png")},
    )
    assert response.status_code == 201
    body = response.json()
    asset_id = UUID(body["asset_id"])
    object_key: str | None = None
    try:
        with factory() as session:
            record = session.get(ReferenceAssetModel, asset_id)
            assert record is not None
            object_key = record.object_key
            assert record.status == "READY"
            assert record.sha256 == digest
            assert record.size_bytes == len(content)
            assert (record.width, record.height) == (512, 512)
        stored = blob_store.stat(object_key)
        assert stored.size_bytes == len(content)
        assert stored.content_type == "image/png"
        assert hashlib.sha256(blob_store.get(object_key)).hexdigest() == digest

        public_endpoint = os.environ.get("MINIO_PUBLIC_ENDPOINT", "http://127.0.0.1:9000")
        bucket = os.environ.get("MINIO_BUCKET", "museflow-results")
        anonymous = httpx.get(f"{public_endpoint.rstrip('/')}/{bucket}/{object_key}", timeout=10)
        assert anonymous.status_code in {403, 404}
        downloaded = client.get(body["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == content
        assert downloaded.headers["content-type"] == "image/png"
        assert "object_key" not in body
    finally:
        if object_key is not None:
            blob_store.delete(object_key)
        with factory.begin() as session:
            session.execute(
                delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == asset_id)
            )
            session.execute(delete(ReferenceAssetModel).where(ReferenceAssetModel.id == asset_id))
