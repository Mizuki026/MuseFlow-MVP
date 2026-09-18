from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from museflow.tasks.domain import (
    CreateTaskRequest,
    TaskPolicy,
    TaskStatus,
    create_queued_task,
    normalize_create_request,
    request_fingerprint,
)
from museflow.tasks.repository import EventRecord, TaskRecord, TaskRepository


class IdempotencyConflictError(ValueError):
    pass


class TaskNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class CreateTaskResult:
    task: TaskRecord
    idempotency_replayed: bool


@dataclass(frozen=True, slots=True)
class TaskDetail:
    task: TaskRecord
    events: list[EventRecord]


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: list[TaskRecord]
    next_cursor: str | None


def encode_cursor(created_at: datetime, task_id: UUID) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(task_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode((cursor + padding).encode("ascii")))
        created_at = datetime.fromisoformat(data["created_at"])
        task_id = UUID(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cursor") from error
    if created_at.tzinfo is None:
        raise ValueError("invalid cursor")
    return created_at, task_id


class CreateTask:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
        *,
        policy: TaskPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()
        self._policy = policy or TaskPolicy(
            max_attempts=3, policy_version="mvp-0.2", deadline_seconds=600
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def execute(self, request: CreateTaskRequest, idempotency_key: str) -> CreateTaskResult:
        normalized = normalize_create_request(request)
        fingerprint = request_fingerprint(normalized)
        existing = self._find_existing(idempotency_key)
        if existing is not None:
            return self._replay_or_conflict(existing, fingerprint)

        task = create_queued_task(
            task_id=self._id_factory(),
            idempotency_key=idempotency_key,
            request=normalized,
            created_at=self._clock(),
            policy=self._policy,
        )
        try:
            with self._session_factory.begin() as session:
                created = self._repository.add_queued_task(session, task)
                self._repository.add_creation_event(session, task)
                self._repository.add_execution_outbox(session, task)
        except IntegrityError:
            winner = self._find_existing(idempotency_key)
            if winner is None:
                raise
            return self._replay_or_conflict(winner, fingerprint)
        return CreateTaskResult(task=created, idempotency_replayed=False)

    def _find_existing(self, idempotency_key: str) -> TaskRecord | None:
        with self._session_factory() as session:
            return self._repository.find_by_idempotency_key(session, idempotency_key)

    @staticmethod
    def _replay_or_conflict(existing: TaskRecord, fingerprint: str) -> CreateTaskResult:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("idempotency key was used for a different request")
        return CreateTaskResult(task=existing, idempotency_replayed=True)


class GetTaskDetail:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()

    def execute(self, task_id: UUID) -> TaskDetail:
        with self._session_factory() as session:
            task = self._repository.find_by_id(session, task_id)
            if task is None:
                raise TaskNotFoundError(str(task_id))
            return TaskDetail(task=task, events=self._repository.list_events(session, task_id))


class ListTasks:
    MAX_LIMIT = 100

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: TaskRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TaskRepository()

    def execute(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        status: TaskStatus | None = None,
    ) -> TaskPage:
        if not 1 <= limit <= self.MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {self.MAX_LIMIT}")
        before = decode_cursor(cursor) if cursor else None
        with self._session_factory() as session:
            records = self._repository.list_page(
                session, limit=limit + 1, before=before, status=status
            )
        has_more = len(records) > limit
        items = records[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        )
        return TaskPage(items=items, next_cursor=next_cursor)
