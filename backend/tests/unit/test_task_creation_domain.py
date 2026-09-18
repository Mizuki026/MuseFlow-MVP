from datetime import datetime, timezone
from uuid import uuid4

from museflow.tasks.domain import (
    CreateTaskRequest,
    TaskPolicy,
    TaskStatus,
    create_queued_task,
    normalize_create_request,
)


def test_create_queued_task_captures_policy_snapshot_and_queued_state() -> None:
    task_id = uuid4()
    created_at = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
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
    assert task.deadline_at == datetime(2026, 9, 18, 12, 10, tzinfo=timezone.utc)
