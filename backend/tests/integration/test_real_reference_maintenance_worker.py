from __future__ import annotations

import hashlib
import io
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import OutboxMessageModel, ReferenceAssetModel
from museflow.db.session import create_session_factory
from museflow.reference_assets.blob_store import MinioBlobStore
from museflow.reference_assets.image_inspector import ImageInspector
from museflow.reference_assets.object_keys import reference_object_key


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 512), (72, 111, 18)).save(output, format="PNG")
    return output.getvalue()


def test_scheduler_dispatches_to_real_isolated_maintenance_worker() -> None:
    if os.environ.get("MUSEFLOW_RUN_REAL_MAINTENANCE_TEST") != "1":
        pytest.skip("set MUSEFLOW_RUN_REAL_MAINTENANCE_TEST=1 with Compose scheduler and workers")
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required")
    factory: sessionmaker[Session] = create_session_factory(database_url)
    blob_store = MinioBlobStore()
    blob_store.check_ready()
    content = _png()
    digest = hashlib.sha256(content).hexdigest()
    # Inspect a local file so the fixture exercises the same decoder contract as uploads.
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(prefix="museflow-maintenance-test-", delete=False) as file:
        image_path = Path(file.name)
        file.write(content)
    try:
        metadata = ImageInspector().inspect(image_path, declared_content_type="image/png")
    finally:
        image_path.unlink(missing_ok=True)

    asset_id = uuid4()
    object_key = reference_object_key(asset_id, metadata.sha256, metadata.content_type)
    now = datetime.now(UTC)
    message_id = uuid4()
    with factory.begin() as session:
        session.add(
            ReferenceAssetModel(
                id=asset_id,
                idempotency_key=f"real-maintenance-{asset_id}",
                request_fingerprint=hashlib.sha256(str(asset_id).encode()).hexdigest(),
                status="STAGING",
                object_key=object_key,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
                width=metadata.width,
                height=metadata.height,
                sha256=metadata.sha256,
                created_at=now - timedelta(hours=2),
            )
        )
        session.add(
            OutboxMessageModel(
                id=message_id,
                message_type="RUN_REFERENCE_MAINTENANCE",
                aggregate_id=uuid4(),
                payload={"operation": "reference_asset_maintenance"},
                available_at=now,
                created_at=now,
            )
        )
    try:
        blob_store.put(
            object_key,
            io.BytesIO(content),
            size_bytes=len(content),
            content_type="image/png",
            sha256=digest,
        )
        deadline = time.monotonic() + 30
        status = "STAGING"
        while time.monotonic() < deadline:
            with factory() as session:
                status = session.scalar(
                    select(ReferenceAssetModel.status).where(ReferenceAssetModel.id == asset_id)
                )
            if status != "STAGING":
                break
            time.sleep(0.25)
        assert status == "READY"
        with factory() as session:
            record = session.get(ReferenceAssetModel, asset_id)
            assert record is not None
            assert record.sha256 == hashlib.sha256(blob_store.get(object_key)).hexdigest()
    finally:
        blob_store.delete(object_key)
        with factory.begin() as session:
            session.execute(delete(OutboxMessageModel).where(OutboxMessageModel.id == message_id))
            session.execute(delete(ReferenceAssetModel).where(ReferenceAssetModel.id == asset_id))
