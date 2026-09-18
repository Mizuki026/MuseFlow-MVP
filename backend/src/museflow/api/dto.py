from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from museflow.tasks.domain import TaskStatus
from museflow.tasks.repository import EventRecord, TaskRecord


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str


class TaskEventResponse(BaseModel):
    id: UUID
    type: str
    created_at: datetime

    @classmethod
    def from_record(cls, event: EventRecord) -> TaskEventResponse:
        return cls(id=event.id, type=event.event_type, created_at=event.created_at)


class TaskSummaryResponse(BaseModel):
    id: UUID
    prompt: str
    size_preset: str
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

    @classmethod
    def from_record(cls, task: TaskRecord) -> TaskSummaryResponse:
        return cls(
            id=task.id,
            prompt=task.prompt,
            size_preset=task.size_preset,
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
        )


class TaskResponse(TaskSummaryResponse):
    idempotency_replayed: bool = False
    events: list[TaskEventResponse] = []


class TaskListResponse(BaseModel):
    items: list[TaskSummaryResponse]
    next_cursor: str | None


class HealthResponse(BaseModel):
    status: str
