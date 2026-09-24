from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from uuid import UUID

import pytest
from PIL import Image

from museflow.reference_assets.access import ReferenceAssetReader, ReferenceAssetReadError
from museflow.reference_assets.blob_store import (
    BlobMetadata,
    BlobNotFoundError,
    BlobStoreUnavailable,
)
from museflow.reference_assets.repository import ReferenceAssetRecord
from museflow.tasks.domain import ReferenceAssetStatus


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Repository:
    def __init__(self, record: ReferenceAssetRecord) -> None:
        self.record = record

    def find(self, _session, asset_id: UUID) -> ReferenceAssetRecord | None:
        return self.record if asset_id == self.record.id else None


class _BlobStore:
    def __init__(self, content: bytes, sha256: str) -> None:
        self.content = content
        self.sha256 = sha256
        self.stat_error: Exception | None = None
        self.get_error: Exception | None = None

    def stat(self, _key: str) -> BlobMetadata:
        if self.stat_error is not None:
            raise self.stat_error
        return BlobMetadata(len(self.content), "image/png", self.sha256)

    def get(self, _key: str, *, max_bytes: int) -> bytes:
        if self.get_error is not None:
            raise self.get_error
        if len(self.content) > max_bytes:
            raise ValueError("object too large")
        return self.content


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (512, 512), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _reader(content: bytes, *, status: ReferenceAssetStatus = ReferenceAssetStatus.READY):
    digest = hashlib.sha256(content).hexdigest()
    record = ReferenceAssetRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        idempotency_key="reader-test",
        request_fingerprint="a" * 64,
        status=status,
        object_key="references/private/object.png",
        content_type="image/png",
        size_bytes=len(content),
        width=512,
        height=512,
        sha256=digest,
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        ready_at=datetime(2026, 9, 24, tzinfo=UTC),
        delete_pending_at=None,
        deleted_at=None,
        error_code=None,
        error_message=None,
        operation_lease_token=None,
        operation_lease_expires_at=None,
    )
    store = _BlobStore(content, digest)
    reader = ReferenceAssetReader(
        lambda: _Session(), store, repository=_Repository(record)
    )
    return reader, store, record


def test_reader_returns_verified_actual_bytes_and_checks_ownership_around_io() -> None:
    content = _png((30, 80, 140))
    reader, _store, record = _reader(content)
    ownership_checks: list[bool] = []

    image = reader.read_for_task(
        record.id,
        expected_sha256=record.sha256 or "",
        ownership_guard=lambda: ownership_checks.append(True),
    )

    assert image.content == content
    assert image.content_type == "image/png"
    assert (image.width, image.height) == (512, 512)
    assert image.sha256 == record.sha256
    assert len(ownership_checks) >= 5


def test_reader_fails_closed_for_changed_bytes_and_missing_objects() -> None:
    expected = _png((30, 80, 140))
    tampered = _png((220, 30, 10))
    reader, store, record = _reader(expected)
    store.content = tampered
    store.sha256 = record.sha256 or ""
    with pytest.raises(ReferenceAssetReadError) as mismatch:
        reader.read_for_task(
            record.id, expected_sha256=record.sha256 or "", ownership_guard=lambda: None
        )
    assert mismatch.value.code == "INPUT_REFERENCE_INVALID"
    assert mismatch.value.retryable is False

    reader, store, record = _reader(expected)
    store.stat_error = BlobNotFoundError("missing")
    with pytest.raises(ReferenceAssetReadError) as missing:
        reader.read_for_task(
            record.id, expected_sha256=record.sha256 or "", ownership_guard=lambda: None
        )
    assert missing.value.code == "INPUT_REFERENCE_OBJECT_MISSING"
    assert missing.value.retryable is False


def test_reader_classifies_temporary_store_errors_as_same_attempt_recovery() -> None:
    reader, store, record = _reader(_png((30, 80, 140)))
    store.get_error = BlobStoreUnavailable("temporary outage")

    with pytest.raises(ReferenceAssetReadError) as error:
        reader.read_for_task(
            record.id, expected_sha256=record.sha256 or "", ownership_guard=lambda: None
        )

    assert error.value.code == "INPUT_STORAGE_UNAVAILABLE"
    assert error.value.retryable is True


def test_reader_requires_ready_state_and_frozen_digest() -> None:
    reader, _store, record = _reader(
        _png((30, 80, 140)), status=ReferenceAssetStatus.DELETE_PENDING
    )
    with pytest.raises(ReferenceAssetReadError) as state_error:
        reader.read_for_task(
            record.id, expected_sha256=record.sha256 or "", ownership_guard=lambda: None
        )
    assert state_error.value.code == "INPUT_REFERENCE_UNAVAILABLE"

    reader, _store, record = _reader(_png((30, 80, 140)))
    with pytest.raises(ReferenceAssetReadError) as digest_error:
        reader.read_for_task(
            record.id, expected_sha256="b" * 64, ownership_guard=lambda: None
        )
    assert digest_error.value.code == "INPUT_REFERENCE_INVALID"
