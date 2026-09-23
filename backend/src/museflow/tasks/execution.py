from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from museflow.assets import ResultAssetStore, StoredAsset
from museflow.db.models import GenerationTaskModel, ResultAssetModel, TaskEventModel
from museflow.providers import (
    GenerationProvider,
    GenerationRequest,
    GenerationResult,
    ProviderError,
)
from museflow.tasks.domain import RetryPolicy, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    task_id: UUID
    attempt_id: UUID
    sequence: int
    execution_token: UUID
    provider_request_key: str
    remote_request_id: str | None
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
        asset_store: ResultAssetStore | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 240,
        token_factory: Callable[[], UUID] = uuid4,
        random_source: Callable[[], float] = random.random,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._asset_store = asset_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._token_factory = token_factory
        self._random_source = random_source
        self._retry_policy = retry_policy or RetryPolicy()

    def execute(self, task_id: UUID) -> ExecutionOutcome:
        claim = self._claim(task_id)
        if claim is None:
            return ExecutionOutcome(task_id, None, executed=False, succeeded=False)
        try:
            result = self._provider.generate(
                claim.request,
                request_key=claim.provider_request_key,
                remote_request_id=claim.remote_request_id,
                on_remote_request_id=lambda remote_id: self._record_provider_request_id(
                    claim, remote_id
                ),
            )
        except Exception as error:
            self._fail(claim, error)
            return ExecutionOutcome(task_id, claim.attempt_id, executed=True, succeeded=False)

        try:
            self._mark_result_persisting(claim)
            stored = self._store_result(claim, result)
        except ValueError as error:
            self._fail(
                claim,
                ProviderError("RESULT_INVALID", str(error), retryable=False),
            )
            return ExecutionOutcome(task_id, claim.attempt_id, executed=True, succeeded=False)
        except Exception:
            self._record_result_storage_failure(claim)
            logger.exception(
                "result storage failed",
                extra={
                    "task_id": str(claim.task_id),
                    "attempt_id": str(claim.attempt_id),
                    "provider_name": getattr(self._provider, "name", "mock"),
                    "error_code": "RESULT_STORAGE_ERROR",
                },
            )
            raise

        # A transport/storage crash is deliberately allowed to escape. The lease and
        # the stable attempt key make the next delivery retry the same attempt.
        committed = self._succeed(claim, result, stored)
        return ExecutionOutcome(task_id, claim.attempt_id, executed=True, succeeded=committed)

    def _store_result(self, claim: ExecutionClaim, result: GenerationResult) -> StoredAsset | None:
        if self._asset_store is None:
            return None
        return self._asset_store.put_result(
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            content=result.content,
            content_type=result.content_type,
        )

    def _mark_result_persisting(self, claim: ExecutionClaim) -> bool:
        now = self._clock()
        with self._session_factory.begin() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            if (
                attempt is None
                or task is None
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
            attempt.phase = "RESULT_PERSISTING"
            task.version += 1
            return True

    def _record_result_storage_failure(self, claim: ExecutionClaim) -> bool:
        now = self._clock()
        error_code = "RESULT_STORAGE_ERROR"
        error_message = "result storage failed; the current attempt will be recovered"
        with self._session_factory.begin() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            if (
                attempt is None
                or task is None
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
            attempt.phase = "RESULT_PERSISTING"
            attempt.error_code = error_code
            attempt.error_message = error_message
            task.error_code = error_code
            task.error_message = error_message
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="RESULT_STORAGE_FAILED",
                    payload={"attempt_id": str(claim.attempt_id), "error_code": error_code},
                    created_at=now,
                )
            )
            return True

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
            if task.deadline_at <= now:
                self._mark_deadline_locked(session, task, now)
                return None
            if task.status == TaskStatus.RETRY_WAIT.value and (
                task.next_attempt_at is None or task.next_attempt_at > now
            ):
                return None

            latest = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.task_id == task_id)
                .order_by(GenerationAttemptModel.sequence.desc())
                .with_for_update()
            )
            if latest is not None and latest.status == "SUCCEEDED":
                return None
            if latest is not None and latest.status == "RUNNING" and latest.lease_expires_at > now:
                return None

            if latest is None or latest.status == "FAILED":
                sequence = 1 if latest is None else latest.sequence + 1
                attempt = GenerationAttemptModel(
                    id=uuid4(),
                    task_id=task_id,
                    sequence=sequence,
                    status="RUNNING",
                    phase="PROVIDER_RUNNING",
                    provider_name=getattr(self._provider, "name", "mock"),
                    provider_request_key=f"{task_id}:attempt:{sequence}",
                    provider_request_id=latest.provider_request_id if latest is not None else None,
                    execution_token=self._token_factory(),
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    started_at=now,
                )
                session.add(attempt)
                event_type = "ATTEMPT_STARTED"
            else:
                attempt = latest
                sequence = attempt.sequence
                attempt.status = "RUNNING"
                attempt.phase = "PROVIDER_RUNNING"
                attempt.execution_token = self._token_factory()
                attempt.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                attempt.error_code = None
                attempt.error_message = None
                attempt.finished_at = None
                event_type = "ATTEMPT_RECLAIMED"

            task.status = TaskStatus.RUNNING.value
            task.next_attempt_at = None
            task.started_at = task.started_at or now
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=task_id,
                    event_type=event_type,
                    payload={"attempt_id": str(attempt.id), "sequence": sequence},
                    created_at=now,
                )
            )
            return ExecutionClaim(
                task_id=task_id,
                attempt_id=attempt.id,
                sequence=sequence,
                execution_token=attempt.execution_token,
                provider_request_key=attempt.provider_request_key,
                remote_request_id=attempt.provider_request_id,
                request=GenerationRequest(
                    prompt=task.prompt,
                    size_preset=task.size_preset,
                    deadline_at=task.deadline_at,
                ),
            )

    def _record_provider_request_id(self, claim: ExecutionClaim, provider_request_id: str) -> None:
        with self._session_factory.begin() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if (
                attempt is not None
                and attempt.execution_token == claim.execution_token
                and attempt.status == "RUNNING"
            ):
                attempt.provider_request_id = provider_request_id

    def _succeed(
        self, claim: ExecutionClaim, result: GenerationResult, stored: StoredAsset | None
    ) -> bool:
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
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
            if (
                stored is not None
                and session.scalar(
                    select(ResultAssetModel).where(ResultAssetModel.attempt_id == claim.attempt_id)
                )
                is None
            ):
                session.add(
                    ResultAssetModel(
                        id=uuid4(),
                        task_id=claim.task_id,
                        attempt_id=claim.attempt_id,
                        role="RESULT",
                        object_key=stored.object_key,
                        content_type=stored.content_type,
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        created_at=now,
                    )
                )
            attempt.status = "SUCCEEDED"
            attempt.phase = "RESULT_PERSISTED"
            attempt.provider_request_id = result.provider_request_id
            attempt.result_digest = result.result_digest
            attempt.result_metadata = asdict(stored) if stored is not None else result.metadata
            attempt.finished_at = now
            task.status = TaskStatus.SUCCEEDED.value
            task.completed_at = now
            task.next_attempt_at = None
            task.error_code = None
            task.error_message = None
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
            return True

    def _fail(self, claim: ExecutionClaim, error: Exception) -> bool:
        now = self._clock()
        code, retryable = self._error_details(error)
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
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
            attempt.status = "FAILED"
            attempt.phase = "PROVIDER_FAILED"
            attempt.error_code = code
            attempt.error_message = str(error)
            attempt.finished_at = now
            next_at = now
            can_retry = retryable and claim.sequence < task.max_attempts
            if can_retry:
                delay = self._retry_policy.delay_seconds(claim.sequence, self._random_source())
                next_at = now + timedelta(seconds=delay)
                can_retry = next_at < task.deadline_at
            if can_retry:
                task.status = TaskStatus.RETRY_WAIT.value
                task.next_attempt_at = next_at
                task.error_code = code
                task.error_message = str(error)
                event_type = "TASK_RETRY_WAIT"
            else:
                terminal_code = (
                    "DEADLINE_EXCEEDED"
                    if now >= task.deadline_at
                    else ("RETRY_EXHAUSTED" if retryable else code)
                )
                task.status = TaskStatus.FAILED.value
                task.error_code = terminal_code
                task.error_message = str(error)
                task.completed_at = now
                task.next_attempt_at = None
                event_type = "TASK_FAILED"
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type=event_type,
                    payload={"attempt_id": str(claim.attempt_id), "error_code": task.error_code},
                    created_at=now,
                )
            )
            return True

    @staticmethod
    def _error_details(error: Exception) -> tuple[str, bool]:
        if isinstance(error, ProviderError):
            return error.code, error.retryable
        return "PROVIDER_UNAVAILABLE", True

    @staticmethod
    def _claim_is_current(
        attempt: GenerationAttemptModel | None,
        task: GenerationTaskModel | None,
        claim: ExecutionClaim,
        now: datetime,
    ) -> bool:
        return bool(
            attempt is not None
            and task is not None
            and attempt.execution_token == claim.execution_token
            and attempt.lease_expires_at > now
            and attempt.status == "RUNNING"
            and task.status not in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value)
        )

    @staticmethod
    def _mark_deadline_locked(session: Session, task: GenerationTaskModel, now: datetime) -> None:
        task.status = TaskStatus.FAILED.value
        task.error_code = "DEADLINE_EXCEEDED"
        task.error_message = "task deadline exceeded"
        task.completed_at = now
        task.next_attempt_at = None
        task.version += 1
        session.add(
            TaskEventModel(
                id=uuid4(),
                task_id=task.id,
                event_type="TASK_FAILED",
                payload={"error_code": "DEADLINE_EXCEEDED"},
                created_at=now,
            )
        )
