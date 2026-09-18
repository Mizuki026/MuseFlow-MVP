from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import GenerationTaskModel, TaskEventModel
from museflow.providers import GenerationProvider, GenerationRequest, GenerationResult
from museflow.tasks.domain import TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    task_id: UUID
    attempt_id: UUID
    sequence: int
    execution_token: UUID
    provider_request_key: str
    request: GenerationRequest


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    task_id: UUID
    attempt_id: UUID | None
    executed: bool
    succeeded: bool


class ExecuteGenerationAttempt:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: GenerationProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 240,
        token_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._token_factory = token_factory

    def execute(self, task_id: UUID) -> ExecutionOutcome:
        claim = self._claim(task_id)
        if claim is None:
            return ExecutionOutcome(task_id, None, executed=False, succeeded=False)
        try:
            result = self._provider.generate(
                claim.request,
                request_key=claim.provider_request_key,
                remote_request_id=None,
            )
        except Exception as error:
            self._fail(claim, error)
            return ExecutionOutcome(task_id, claim.attempt_id, executed=True, succeeded=False)
        self._succeed(claim, result)
        return ExecutionOutcome(task_id, claim.attempt_id, executed=True, succeeded=True)

    def _claim(self, task_id: UUID) -> ExecutionClaim | None:
        now = self._clock()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(GenerationTaskModel)
                .where(GenerationTaskModel.id == task_id)
                .with_for_update()
            )
            if task is None or task.status in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value):
                return None
            latest = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.task_id == task_id)
                .order_by(GenerationAttemptModel.sequence.desc())
                .with_for_update()
            )
            if latest is not None and latest.status == "RUNNING" and latest.lease_expires_at > now:
                return None

            if latest is None:
                sequence = 1
                attempt_id = uuid4()
                provider_request_key = f"{task_id}:attempt:{sequence}"
                attempt = GenerationAttemptModel(
                    id=attempt_id,
                    task_id=task_id,
                    sequence=sequence,
                    status="RUNNING",
                    phase="PROVIDER_RUNNING",
                    provider_name="mock",
                    provider_request_key=provider_request_key,
                    execution_token=self._token_factory(),
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    started_at=now,
                )
                session.add(attempt)
            else:
                sequence = latest.sequence
                attempt_id = latest.id
                provider_request_key = latest.provider_request_key
                latest.status = "RUNNING"
                latest.phase = "PROVIDER_RUNNING"
                latest.execution_token = self._token_factory()
                latest.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                latest.error_code = None
                latest.error_message = None
                latest.finished_at = None
                attempt = latest

            task.status = TaskStatus.RUNNING.value
            task.started_at = task.started_at or now
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=task_id,
                    event_type="ATTEMPT_STARTED" if latest is None else "ATTEMPT_RECLAIMED",
                    payload={"attempt_id": str(attempt_id), "sequence": sequence},
                    created_at=now,
                )
            )
            return ExecutionClaim(
                task_id=task_id,
                attempt_id=attempt_id,
                sequence=sequence,
                execution_token=attempt.execution_token,
                provider_request_key=provider_request_key,
                request=GenerationRequest(prompt=task.prompt, size_preset=task.size_preset),
            )

    def _succeed(self, claim: ExecutionClaim, result: GenerationResult) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.id == claim.attempt_id)
                .with_for_update()
            )
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            if (
                attempt is None
                or task is None
                or attempt.execution_token != claim.execution_token
                or task.status in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value)
            ):
                return
            attempt.status = "SUCCEEDED"
            attempt.phase = "PROVIDER_SUCCEEDED"
            attempt.provider_request_id = result.provider_request_id
            attempt.result_digest = result.result_digest
            attempt.result_metadata = result.metadata
            attempt.finished_at = now
            task.status = TaskStatus.SUCCEEDED.value
            task.completed_at = now
            task.next_attempt_at = None
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="TASK_SUCCEEDED",
                    payload={
                        "attempt_id": str(claim.attempt_id),
                        "result_digest": result.result_digest,
                    },
                    created_at=now,
                )
            )

    def _fail(self, claim: ExecutionClaim, error: Exception) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.id == claim.attempt_id)
                .with_for_update()
            )
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            if (
                attempt is None
                or task is None
                or attempt.execution_token != claim.execution_token
                or task.status in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value)
            ):
                return
            attempt.status = "FAILED"
            attempt.phase = "PROVIDER_FAILED"
            attempt.error_code = "PROVIDER_ERROR"
            attempt.error_message = str(error)
            attempt.finished_at = now
            task.status = TaskStatus.FAILED.value
            task.error_code = "PROVIDER_ERROR"
            task.error_message = str(error)
            task.completed_at = now
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="TASK_FAILED",
                    payload={"attempt_id": str(claim.attempt_id), "error_code": "PROVIDER_ERROR"},
                    created_at=now,
                )
            )
