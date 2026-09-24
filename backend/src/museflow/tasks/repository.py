from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.tasks.domain import GenerationType, QueuedTask, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: UUID
    idempotency_key: str
    request_fingerprint: str
    prompt: str
    size_preset: str
    status: TaskStatus
    max_attempts: int
    policy_version: str
    deadline_at: datetime
    next_attempt_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    version: int
    retried_from_task_id: UUID | None
    execution_profile: str | None
    generation_type: GenerationType
    reference_asset_id: UUID | None
    reference_sha256: str | None
    provider_profile: str
    provider_name: str
    model_name: str
    capability_version: str
    policy_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: UUID
    task_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: UUID
    message_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    available_at: datetime
    published_at: datetime | None
    created_at: datetime


def _task_record(model: GenerationTaskModel) -> TaskRecord:
    return TaskRecord(
        id=model.id,
        idempotency_key=model.idempotency_key,
        request_fingerprint=model.request_fingerprint,
        prompt=model.prompt,
        size_preset=model.size_preset,
        status=TaskStatus(model.status),
        max_attempts=model.max_attempts,
        policy_version=model.policy_version,
        deadline_at=model.deadline_at,
        next_attempt_at=model.next_attempt_at,
        error_code=model.error_code,
        error_message=model.error_message,
        created_at=model.created_at,
        queued_at=model.queued_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
        version=model.version,
        retried_from_task_id=model.retried_from_task_id,
        execution_profile=model.execution_profile,
        generation_type=GenerationType(model.generation_type),
        reference_asset_id=model.reference_asset_id,
        reference_sha256=model.reference_sha256,
        provider_profile=model.provider_profile,
        provider_name=model.provider_name,
        model_name=model.model_name,
        capability_version=model.capability_version,
        policy_snapshot=model.policy_snapshot,
    )


def _event_record(model: TaskEventModel) -> EventRecord:
    return EventRecord(
        id=model.id,
        task_id=model.task_id,
        event_type=model.event_type,
        payload=model.payload,
        created_at=model.created_at,
    )


def _outbox_record(model: OutboxMessageModel) -> OutboxRecord:
    return OutboxRecord(
        id=model.id,
        message_type=model.message_type,
        aggregate_id=model.aggregate_id,
        payload=model.payload,
        available_at=model.available_at,
        published_at=model.published_at,
        created_at=model.created_at,
    )


class TaskRepository:
    def find_by_idempotency_key(self, session: Session, key: str) -> TaskRecord | None:
        model = session.scalar(
            select(GenerationTaskModel).where(GenerationTaskModel.idempotency_key == key)
        )
        return _task_record(model) if model else None

    def add_queued_task(self, session: Session, task: QueuedTask) -> TaskRecord:
        model = GenerationTaskModel(
            id=task.id,
            idempotency_key=task.idempotency_key,
            request_fingerprint=task.request_fingerprint,
            prompt=task.prompt,
            size_preset=task.size_preset,
            status=task.status.value,
            max_attempts=task.max_attempts,
            policy_version=task.policy_version,
            deadline_at=task.deadline_at,
            created_at=task.created_at,
            queued_at=task.queued_at,
            version=1,
            retried_from_task_id=task.retried_from_task_id,
            execution_profile=task.execution_profile,
            generation_type=task.generation_type.value,
            reference_asset_id=task.reference_asset_id,
            reference_sha256=task.reference_sha256,
            provider_profile=task.provider_profile,
            provider_name=task.provider_name,
            model_name=task.model_name,
            capability_version=task.capability_version,
            policy_snapshot=task.policy_snapshot,
        )
        session.add(model)
        session.flush()
        return _task_record(model)

    def add_creation_event(self, session: Session, task: QueuedTask) -> EventRecord:
        model = TaskEventModel(
            id=uuid4(),
            task_id=task.id,
            event_type="TASK_QUEUED",
            payload={"status": TaskStatus.QUEUED.value},
            created_at=task.created_at,
        )
        session.add(model)
        session.flush()
        return _event_record(model)

    def add_execution_outbox(self, session: Session, task: QueuedTask) -> OutboxRecord:
        model = OutboxMessageModel(
            id=uuid4(),
            message_type="EXECUTE_TASK",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
            available_at=task.created_at,
            created_at=task.created_at,
        )
        session.add(model)
        session.flush()
        return _outbox_record(model)

    def find_by_id(self, session: Session, task_id: UUID) -> TaskRecord | None:
        model = session.get(GenerationTaskModel, task_id)
        return _task_record(model) if model else None

    def list_events(self, session: Session, task_id: UUID) -> list[EventRecord]:
        statement = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .order_by(TaskEventModel.created_at.asc(), TaskEventModel.id.asc())
        )
        return [_event_record(model) for model in session.scalars(statement)]

    def list_outbox(self, session: Session, task_id: UUID) -> list[OutboxRecord]:
        statement = select(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
        return [_outbox_record(model) for model in session.scalars(statement)]

    def list_page(
        self,
        session: Session,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None = None,
        status: TaskStatus | None = None,
    ) -> list[TaskRecord]:
        statement: Select[tuple[GenerationTaskModel]] = select(GenerationTaskModel)
        if status is not None:
            statement = statement.where(GenerationTaskModel.status == status.value)
        if before is not None:
            created_at, task_id = before
            statement = statement.where(
                (GenerationTaskModel.created_at < created_at)
                | (
                    (GenerationTaskModel.created_at == created_at)
                    & (GenerationTaskModel.id < task_id)
                )
            )
        statement = statement.order_by(
            GenerationTaskModel.created_at.desc(), GenerationTaskModel.id.desc()
        ).limit(limit)
        return [_task_record(model) for model in session.scalars(statement)]
