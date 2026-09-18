from __future__ import annotations

from uuid import uuid4

import pytest

from museflow.assets import deterministic_object_key, validate_result
from museflow.tasks.domain import RetryPolicy


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


def test_result_object_key_is_stable_for_same_authoritative_attempt() -> None:
    task_id = uuid4()
    attempt_id = uuid4()

    assert deterministic_object_key(task_id, attempt_id, "image/png") == (
        f"results/{task_id}/{attempt_id}/0.png"
    )
