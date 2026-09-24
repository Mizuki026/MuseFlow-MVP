from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from museflow.tasks.domain import (
    CreateTaskRequest,
    DomainErrorCode,
    DomainValidationError,
    GenerationType,
    ImageToImageInput,
    TaskPolicy,
    TaskStatus,
    create_queued_task,
    normalize_create_request,
    request_fingerprint,
)


def test_create_queued_task_captures_policy_snapshot_and_queued_state() -> None:
    task_id = uuid4()
    created_at = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    request = normalize_create_request(CreateTaskRequest(prompt="a quiet forest"))

    task = create_queued_task(
        task_id=task_id,
        idempotency_key="request-1",
        request=request,
        created_at=created_at,
        policy=TaskPolicy(max_attempts=3, policy_version="2026-09-18", deadline_seconds=600),
    )

    assert task.id == task_id
    assert task.status is TaskStatus.QUEUED
    assert task.prompt == "a quiet forest"
    assert task.max_attempts == 3
    assert task.policy_version == "2026-09-18"
    assert task.deadline_at == datetime(2026, 9, 18, 12, 10, tzinfo=UTC)
    assert task.generation_type is GenerationType.TEXT_TO_IMAGE
    assert task.policy_snapshot["frozen"] is True
    assert task.policy_snapshot["retry_policy"] == {
        "initial_delay_seconds": 2.0,
        "multiplier": 2.0,
        "max_delay_seconds": 30.0,
    }


def test_omitted_and_explicit_text_generation_type_have_the_same_normalized_fingerprint() -> None:
    omitted = normalize_create_request(CreateTaskRequest(prompt="same request"))
    explicit = normalize_create_request(
        CreateTaskRequest(prompt="same request", generation_type=GenerationType.TEXT_TO_IMAGE)
    )

    assert omitted == explicit


def test_image_to_image_requires_one_reference_and_normalizes_server_digest() -> None:
    with pytest.raises(DomainValidationError) as error:
        normalize_create_request(
            CreateTaskRequest(prompt="edit this", generation_type=GenerationType.IMAGE_TO_IMAGE)
        )

    assert error.value.code is DomainErrorCode.REFERENCE_ASSET_REQUIRED

    reference_id = UUID("00000000-0000-0000-0000-000000000001")
    request = normalize_create_request(
        CreateTaskRequest(
            prompt="edit this",
            generation_type=GenerationType.IMAGE_TO_IMAGE,
            reference_asset_id=reference_id,
        ),
        reference_sha256="a" * 64,
    )
    assert isinstance(request.input, ImageToImageInput)
    assert request.input.reference_asset_id == reference_id
    assert request.input.reference_sha256 == "a" * 64


def test_text_to_image_does_not_accept_reference_asset_fields() -> None:
    with pytest.raises(DomainValidationError) as error:
        normalize_create_request(CreateTaskRequest(prompt="draw this", reference_asset_id=uuid4()))

    assert error.value.code is DomainErrorCode.REFERENCE_ASSET_NOT_ALLOWED


def test_generation_type_and_reference_identity_are_in_the_stable_fingerprint() -> None:
    reference_id = UUID("00000000-0000-0000-0000-000000000001")
    first = normalize_create_request(
        CreateTaskRequest(
            prompt="edit",
            generation_type=GenerationType.IMAGE_TO_IMAGE,
            reference_asset_id=reference_id,
        ),
        reference_sha256="a" * 64,
    )
    changed_id = normalize_create_request(
        CreateTaskRequest(
            prompt="edit",
            generation_type=GenerationType.IMAGE_TO_IMAGE,
            reference_asset_id=UUID("00000000-0000-0000-0000-000000000002"),
        ),
        reference_sha256="a" * 64,
    )
    changed_sha = normalize_create_request(
        CreateTaskRequest(
            prompt="edit",
            generation_type=GenerationType.IMAGE_TO_IMAGE,
            reference_asset_id=reference_id,
        ),
        reference_sha256="b" * 64,
    )

    assert request_fingerprint(first) == (
        "87cc463dcc67e78f0c68045a0bed79a03d7a2b0f5bcf0bff08fa26224dfbed5e"
    )
    assert request_fingerprint(first) != request_fingerprint(changed_id)
    assert request_fingerprint(first) != request_fingerprint(changed_sha)
    assert request_fingerprint(
        normalize_create_request(CreateTaskRequest(prompt="same request"))
    ) == ("84f63ef52ccba93e4dfc16712b4b16d296bd9d6da8906ee9618ae3989a685e53")
