from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DomainErrorCode(StrEnum):
    INVALID_PROMPT = "INVALID_PROMPT"
    INVALID_GENERATION_OPTIONS = "INVALID_GENERATION_OPTIONS"


class DomainValidationError(ValueError):
    def __init__(self, code: DomainErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    prompt: str
    size_preset: str | None = None
    image_count: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedCreateTaskRequest:
    prompt: str
    size_preset: str
    image_count: int


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    max_attempts: int
    policy_version: str
    deadline_seconds: int


@dataclass(frozen=True, slots=True)
class QueuedTask:
    id: UUID
    idempotency_key: str
    request_fingerprint: str
    prompt: str
    size_preset: str
    status: TaskStatus
    max_attempts: int
    policy_version: str
    deadline_at: datetime
    created_at: datetime
    queued_at: datetime


def normalize_create_request(request: CreateTaskRequest) -> NormalizedCreateTaskRequest:
    if not request.prompt.strip():
        raise DomainValidationError(
            DomainErrorCode.INVALID_PROMPT,
            "prompt must contain at least one non-whitespace character",
        )
    if not 1 <= len(request.prompt) <= 2000:
        raise DomainValidationError(
            DomainErrorCode.INVALID_PROMPT,
            "prompt length must be between 1 and 2000 characters",
        )
    if request.size_preset not in (None, "1280*1280") or request.image_count not in (None, 1):
        raise DomainValidationError(
            DomainErrorCode.INVALID_GENERATION_OPTIONS,
            "only the 1280*1280 single-image preset is supported",
        )
    return NormalizedCreateTaskRequest(
        prompt=request.prompt,
        size_preset="1280*1280",
        image_count=1,
    )


def request_fingerprint(request: NormalizedCreateTaskRequest) -> str:
    canonical_request = json.dumps(
        {
            "image_count": request.image_count,
            "prompt": request.prompt,
            "size_preset": request.size_preset,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_request).hexdigest()


def create_queued_task(
    *,
    task_id: UUID,
    idempotency_key: str,
    request: NormalizedCreateTaskRequest,
    created_at: datetime,
    policy: TaskPolicy,
) -> QueuedTask:
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    if policy.max_attempts < 1 or policy.deadline_seconds < 1:
        raise ValueError("task policy must allow at least one attempt and one second")
    return QueuedTask(
        id=task_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint(request),
        prompt=request.prompt,
        size_preset=request.size_preset,
        status=TaskStatus.QUEUED,
        max_attempts=policy.max_attempts,
        policy_version=policy.policy_version,
        deadline_at=created_at + timedelta(seconds=policy.deadline_seconds),
        created_at=created_at,
        queued_at=created_at,
    )
