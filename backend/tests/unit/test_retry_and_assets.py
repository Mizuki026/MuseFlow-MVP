from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from museflow.assets import candidate_object_key, result_identity, validate_result
from museflow.tasks.domain import RetryPolicy
from museflow.tasks.result_publication import (
    PublicationDecision,
    ResultPointer,
    decide_result_publication,
)


def test_retry_policy_is_exponential_full_jitter_and_bounded() -> None:
    policy = RetryPolicy()

    assert policy.delay_seconds(1, 0.5) == 1.0
    assert policy.delay_seconds(2, 1.0) == 4.0
    assert policy.delay_seconds(8, 1.0) == 30.0


def test_result_validation_records_checksum_and_rejects_mismatched_header() -> None:
    content = b"\x89PNG\r\n\x1a\nvalid-payload"

    content_type, size, checksum = validate_result(content, "image/png")

    assert content_type == "image/png"
    assert size == len(content)
    assert len(checksum) == 64
    with pytest.raises(ValueError, match="header"):
        validate_result(b"not-an-image", "image/png")


def test_result_candidate_key_uses_task_attempt_content_digest_and_detected_format() -> None:
    task_id = uuid4()
    attempt_id = uuid4()
    content = b"\x89PNG\r\n\x1a\nactual png bytes"
    identity = result_identity(content, "image/png")

    assert identity.sha256 == hashlib.sha256(content).hexdigest()
    assert candidate_object_key(task_id, attempt_id, identity) == (
        f"results/{task_id}/{attempt_id}/candidates/{identity.sha256}.png"
    )


def test_same_candidate_content_is_stable_and_different_content_gets_different_key() -> None:
    task_id = uuid4()
    attempt_id = uuid4()
    first = result_identity(b"\x89PNG\r\n\x1a\nfirst", "image/png")
    repeated = result_identity(b"\x89PNG\r\n\x1a\nfirst", "image/png")
    second = result_identity(b"\x89PNG\r\n\x1a\nsecond", "image/png")

    assert candidate_object_key(task_id, attempt_id, first) == candidate_object_key(
        task_id, attempt_id, repeated
    )
    assert candidate_object_key(task_id, attempt_id, first) != candidate_object_key(
        task_id, attempt_id, second
    )


def test_candidate_extension_comes_from_verified_bytes_not_untrusted_media_claim() -> None:
    task_id = uuid4()
    attempt_id = uuid4()
    jpeg = result_identity(b"\xff\xd8\xffjpeg bytes", "image/jpeg")

    assert candidate_object_key(task_id, attempt_id, jpeg).endswith(
        f"/{hashlib.sha256(b'\xff\xd8\xffjpeg bytes').hexdigest()}.jpg"
    )
    with pytest.raises(ValueError, match="does not match"):
        result_identity(b"\x89PNG\r\n\x1a\npng bytes", "image/jpeg")
    with pytest.raises(ValueError, match="unsupported"):
        result_identity(b"\x89PNG\r\n\x1a\npng bytes", "image/x-untrusted")


def test_publication_fences_a_claim_with_a_mismatched_execution_token() -> None:
    claim_attempt_id = uuid4()
    candidate = ResultPointer(
        attempt_id=claim_attempt_id,
        object_key="results/task/attempt/candidates/digest.png",
        content_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
    )

    decision = decide_result_publication(
        token_matches=False,
        lease_active=True,
        attempt_status="RUNNING",
        task_status="RUNNING",
        claim_attempt_id=claim_attempt_id,
        existing=None,
        candidate=candidate,
    )

    assert decision is PublicationDecision.OWNERSHIP_LOST


def test_publication_of_the_same_authoritative_pointer_is_idempotent() -> None:
    attempt_id = uuid4()
    pointer = ResultPointer(
        attempt_id=attempt_id,
        object_key="results/task/attempt/candidates/digest.png",
        content_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
    )

    decision = decide_result_publication(
        token_matches=True,
        lease_active=False,
        attempt_status="SUCCEEDED",
        task_status="SUCCEEDED",
        claim_attempt_id=attempt_id,
        existing=pointer,
        candidate=pointer,
    )

    assert decision is PublicationDecision.ALREADY_PUBLISHED


def test_publication_never_replaces_a_different_authoritative_pointer() -> None:
    attempt_id = uuid4()
    existing = ResultPointer(
        attempt_id=uuid4(),
        object_key="results/task/old/candidates/old.png",
        content_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
    )
    candidate = ResultPointer(
        attempt_id=attempt_id,
        object_key="results/task/new/candidates/new.png",
        content_type="image/png",
        size_bytes=12,
        sha256="b" * 64,
    )

    decision = decide_result_publication(
        token_matches=True,
        lease_active=True,
        attempt_status="RUNNING",
        task_status="RUNNING",
        claim_attempt_id=attempt_id,
        existing=existing,
        candidate=candidate,
    )

    assert decision is PublicationDecision.CONFLICTING_RESULT
