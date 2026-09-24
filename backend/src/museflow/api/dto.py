from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from museflow.providers import MockScenario
from museflow.tasks.domain import GenerationType, ReferenceAssetStatus, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.repository import EventRecord, TaskRecord


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    generation_type: GenerationType = GenerationType.TEXT_TO_IMAGE
    size_preset: str | None = None
    reference_asset_id: UUID | None = None


class DemoCreateTaskBody(CreateTaskBody):
    scenario: MockScenario


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ApiErrorDetail


class ReferenceAssetResponse(BaseModel):
    asset_id: UUID
    status: ReferenceAssetStatus
    content_type: str | None
    width: int | None
    height: int | None
    size_bytes: int | None
    sha256: str | None
    download_url: str
    idempotency_replayed: bool = False


class TaskEventResponse(BaseModel):
    id: UUID
    type: str
    created_at: datetime

    @classmethod
    def from_record(cls, event: EventRecord) -> TaskEventResponse:
        return cls(id=event.id, type=event.event_type, created_at=event.created_at)


class TaskAttemptResponse(BaseModel):
    id: UUID
    sequence: int
    status: str
    phase: str
    provider_name: str
    started_at: datetime
    finished_at: datetime | None
    result_digest: str | None

    @classmethod
    def from_model(cls, attempt: GenerationAttemptModel) -> TaskAttemptResponse:
        return cls(
            id=attempt.id,
            sequence=attempt.sequence,
            status=attempt.status,
            phase=attempt.phase,
            provider_name=attempt.provider_name,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            result_digest=attempt.result_digest,
        )


class TaskAssetResponse(BaseModel):
    id: UUID
    role: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    sha256: str
    download_url: str | None = None


class TaskSummaryResponse(BaseModel):
    id: UUID
    prompt: str
    size_preset: str
    generation_type: GenerationType | None = None
    status: TaskStatus
    max_attempts: int
    deadline_at: datetime
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    next_attempt_at: datetime | None
    error_code: str | None
    error_message: str | None
    retried_from_task_id: UUID | None
    thumbnail_url: str | None = None

    @classmethod
    def from_record(cls, task: TaskRecord) -> TaskSummaryResponse:
        return cls(
            id=task.id,
            prompt=task.prompt,
            size_preset=task.size_preset,
            generation_type=task.generation_type,
            status=task.status,
            max_attempts=task.max_attempts,
            deadline_at=task.deadline_at,
            created_at=task.created_at,
            queued_at=task.queued_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
            next_attempt_at=task.next_attempt_at,
            error_code=task.error_code,
            error_message=task.error_message,
            retried_from_task_id=task.retried_from_task_id,
        )


class TextToImageInputResponse(BaseModel):
    generation_type: Literal["TEXT_TO_IMAGE"]
    prompt: str
    size_preset: str


class ImageToImageInputResponse(BaseModel):
    generation_type: Literal["IMAGE_TO_IMAGE"]
    prompt: str
    size_preset: str
    reference_asset_id: UUID
    reference_sha256: str


class TaskResponse(TaskSummaryResponse):
    idempotency_replayed: bool = False
    events: list[TaskEventResponse] = []
    attempts: list[TaskAttemptResponse] = []
    result: TaskAssetResponse | None = None
    retry_task_id: UUID | None = None
    input_summary: (
        Annotated[
            TextToImageInputResponse | ImageToImageInputResponse,
            Field(discriminator="generation_type"),
        ]
        | None
    ) = None
    reference_asset_id: UUID | None = None
    reference_sha256: str | None = None
    reference_download_url: str | None = None
    provider_profile: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    capability_version: str | None = None
    policy_snapshot: dict[str, object] | None = None


class TaskListResponse(BaseModel):
    items: list[TaskSummaryResponse]
    next_cursor: str | None


class HealthResponse(BaseModel):
    status: str
