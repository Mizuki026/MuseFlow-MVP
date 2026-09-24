from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import GenerationTaskModel, OutboxMessageModel, TaskEventModel
from museflow.tasks.domain import TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.execution_semantics import AttemptPhase, submission_outcome_is_unknown


class ScheduleDueRetries:
    def __init__(
        self, session_factory: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, *, limit: int = 100) -> int:
        now = self._clock()
        changed = 0
        with self._session_factory.begin() as session:
            tasks = list(
                session.scalars(
                    select(GenerationTaskModel)
                    .where(
                        GenerationTaskModel.status == TaskStatus.RETRY_WAIT.value,
                        GenerationTaskModel.next_attempt_at <= now,
                    )
                    .order_by(
                        GenerationTaskModel.next_attempt_at.asc(), GenerationTaskModel.id.asc()
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for task in tasks:
                task.status = TaskStatus.QUEUED.value
                task.next_attempt_at = None
                task.version += 1
                self._enqueue(session, task, now)
                session.add(
                    TaskEventModel(
                        id=uuid4(),
                        task_id=task.id,
                        event_type="RETRY_DUE",
                        payload={"status": TaskStatus.QUEUED.value},
                        created_at=now,
                    )
                )
                changed += 1
        return changed

    @staticmethod
    def _enqueue(session: Session, task: GenerationTaskModel, now: datetime) -> None:
        session.add(
            OutboxMessageModel(
                id=uuid4(),
                message_type="EXECUTE_TASK",
                aggregate_id=task.id,
                payload={"task_id": str(task.id)},
                available_at=now,
                created_at=now,
            )
        )


class RecoverExpiredLeases:
    def __init__(
        self, session_factory: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, *, limit: int = 100) -> int:
        now = self._clock()
        changed = 0
        with self._session_factory.begin() as session:
            tasks = list(
                session.scalars(
                    select(GenerationTaskModel)
                    .where(GenerationTaskModel.status == TaskStatus.RUNNING.value)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for task in tasks:
                attempt = session.scalar(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task.id)
                    .order_by(GenerationAttemptModel.sequence.desc())
                )
                if attempt is None or attempt.status != "RUNNING" or attempt.lease_expires_at > now:
                    continue
                submission_unknown = submission_outcome_is_unknown(
                    AttemptPhase(attempt.phase),
                    has_remote_request_id=attempt.provider_request_id is not None,
                )
                if task.deadline_at <= now or submission_unknown:
                    error_code = (
                        "PROVIDER_SUBMISSION_UNKNOWN"
                        if submission_unknown
                        else "DEADLINE_EXCEEDED"
                    )
                    error_message = (
                        "provider may have accepted the creation request; "
                        "automatic resubmission is disabled"
                        if submission_unknown
                        else "task deadline exceeded"
                    )
                    attempt.status = "FAILED"
                    attempt.error_code = error_code
                    attempt.error_message = error_message
                    attempt.finished_at = now
                    task.status = TaskStatus.FAILED.value
                    task.error_code = error_code
                    task.error_message = error_message
                    task.completed_at = now
                    task.next_attempt_at = None
                    event_type = "TASK_FAILED"
                else:
                    task.status = TaskStatus.QUEUED.value
                    task.next_attempt_at = None
                    self._enqueue(session, task, now)
                    event_type = "LEASE_EXPIRED"
                task.version += 1
                session.add(
                    TaskEventModel(
                        id=uuid4(),
                        task_id=task.id,
                        event_type=event_type,
                    payload={
                        "attempt_id": str(attempt.id),
                        "error_code": task.error_code,
                        "deadline_exceeded": task.deadline_at <= now,
                        "possible_external_call": submission_unknown,
                    },
                        created_at=now,
                    )
                )
                changed += 1
        return changed

    @staticmethod
    def _enqueue(session: Session, task: GenerationTaskModel, now: datetime) -> None:
        session.add(
            OutboxMessageModel(
                id=uuid4(),
                message_type="EXECUTE_TASK",
                aggregate_id=task.id,
                payload={"task_id": str(task.id)},
                available_at=now,
                created_at=now,
            )
        )
