from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from museflow.assets import (
    ResultAssetStore,
    StoredAsset,
    candidate_object_key,
    result_identity,
)
from museflow.db.models import GenerationTaskModel, ResultAssetModel, TaskEventModel
from museflow.providers import (
    GenerationProvider,
    GenerationResult,
    ImageToImageProviderInput,
    ProviderError,
    ProviderGenerationRequest,
    ProviderSubmissionUnknownError,
    TextToImageProviderInput,
)
from museflow.reference_assets.access import ReferenceAssetReader, ReferenceAssetReadError
from museflow.tasks.domain import (
    GenerationType,
    ImageToImageInput,
    RetryPolicy,
    TaskInput,
    TaskStatus,
    TextToImageInput,
)
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.execution_semantics import (
    AttemptPhase,
    FailureAction,
    FailureDomain,
    LeaseSettings,
    can_schedule_new_attempt,
    classify_failure_domain,
    decide_failure_action,
    next_phase_for_recovery,
    phase_transition_allowed,
    submission_outcome_is_unknown,
)
from museflow.tasks.lease_guard import (
    LeaseGuard,
    LeaseState,
    OwnershipLostError,
    TaskDeadlineExceededError,
)
from museflow.tasks.result_publication import (
    PublicationDecision,
    ResultPointer,
    ResultPublicationStatus,
    decide_result_publication,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    task_id: UUID
    attempt_id: UUID
    sequence: int
    execution_token: UUID
    phase: AttemptPhase
    provider_request_key: str
    remote_request_id: str | None
    input: TaskInput
    deadline_at: datetime
    lease_settings: LeaseSettings
    retry_policy: RetryPolicy


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    task_id: UUID
    attempt_id: UUID | None
    executed: bool
    succeeded: bool
    publication_status: ResultPublicationStatus | None = None
    candidate_persisted: bool = False
    failure_code: str | None = None


class ExecuteGenerationAttempt:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: GenerationProvider,
        *,
        asset_store: ResultAssetStore | None = None,
        reference_reader: ReferenceAssetReader | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int | None = None,
        heartbeat_interval_seconds: float | None = None,
        lease_settings: LeaseSettings | None = None,
        token_factory: Callable[[], UUID] = uuid4,
        random_source: Callable[[], float] = random.random,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._asset_store = asset_store
        self._reference_reader = reference_reader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_settings_override = lease_settings
        if lease_settings is None and (
            lease_seconds is not None or heartbeat_interval_seconds is not None
        ):
            effective_lease_seconds = lease_seconds or 240
            heartbeat = heartbeat_interval_seconds or min(30.0, effective_lease_seconds / 3)
            lease_settings = LeaseSettings(effective_lease_seconds, heartbeat)
            self._lease_settings_override = lease_settings
        self._fallback_lease_settings = lease_settings or LeaseSettings.from_environment()
        self._token_factory = token_factory
        self._random_source = random_source
        self._retry_policy_override = retry_policy
        self._fallback_retry_policy = retry_policy or RetryPolicy()

    def execute(self, task_id: UUID) -> ExecutionOutcome:
        claim = self._claim(task_id)
        if claim is None:
            return ExecutionOutcome(task_id, None, executed=False, succeeded=False)
        guard = LeaseGuard(
            self._session_factory,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            execution_token=claim.execution_token,
            deadline_at=claim.deadline_at,
            settings=claim.lease_settings,
            clock=self._clock,
        )
        current_phase = claim.phase
        current_remote_id = claim.remote_request_id
        try:
            with guard:
                if claim.phase is AttemptPhase.INPUT_LOADING:
                    self._set_phase(claim, AttemptPhase.INPUT_LOADING, guard)

                try:
                    provider_request = self._provider_request(claim, guard)
                except ReferenceAssetReadError as error:
                    return self._handle_failure(
                        claim,
                        guard,
                        error,
                        phase=AttemptPhase.INPUT_LOADING,
                        remote_request_id=current_remote_id,
                    )

                def on_phase(phase: AttemptPhase) -> None:
                    nonlocal current_phase
                    if tuple(AttemptPhase).index(phase) < tuple(AttemptPhase).index(current_phase):
                        guard.require_ownership()
                        return
                    self._set_phase(claim, phase, guard)
                    current_phase = phase

                def on_remote_request_id(remote_id: str) -> None:
                    nonlocal current_remote_id, current_phase
                    self._record_provider_request_id(claim, remote_id, guard)
                    current_remote_id = remote_id
                    current_phase = AttemptPhase.PROVIDER_RUNNING

                try:
                    result = self._provider.generate(
                        provider_request,
                        request_key=claim.provider_request_key,
                        remote_request_id=claim.remote_request_id,
                        on_remote_request_id=on_remote_request_id,
                        on_phase=on_phase,
                        lease_guard=guard,
                    )
                except Exception as error:
                    if guard.state is LeaseState.OWNED:
                        guard.check()
                    if guard.state is LeaseState.OWNERSHIP_LOST:
                        return self._outcome(claim, succeeded=False)
                    if guard.state is LeaseState.DEADLINE_EXCEEDED:
                        self._mark_deadline(claim)
                        return self._outcome(claim, succeeded=False, error_code="DEADLINE_EXCEEDED")
                    if (
                        not isinstance(error, ProviderError)
                        and current_phase is AttemptPhase.PROVIDER_SUBMITTING
                        and current_remote_id is None
                    ):
                        error = ProviderSubmissionUnknownError()
                    return self._handle_failure(
                        claim,
                        guard,
                        error,
                        phase=current_phase,
                        remote_request_id=current_remote_id,
                    )

                try:
                    guard.require_ownership()
                    self._set_phase(claim, AttemptPhase.RESULT_PERSISTING, guard)
                    current_phase = AttemptPhase.RESULT_PERSISTING
                except OwnershipLostError:
                    return self._outcome(claim, succeeded=False)
                except TaskDeadlineExceededError:
                    self._mark_deadline(claim)
                    return self._outcome(claim, succeeded=False, error_code="DEADLINE_EXCEEDED")

                try:
                    guard.require_ownership()
                    stored = self._store_result(claim, result)
                    guard.require_ownership()
                except ValueError as error:
                    return self._handle_failure(
                        claim,
                        guard,
                        ProviderError("RESULT_INVALID", str(error), retryable=False),
                        phase=AttemptPhase.RESULT_PERSISTING,
                        remote_request_id=current_remote_id,
                    )
                except Exception:
                    if guard.state is LeaseState.OWNERSHIP_LOST:
                        return self._outcome(claim, succeeded=False)
                    if guard.state is LeaseState.DEADLINE_EXCEEDED:
                        self._mark_deadline(claim)
                        return self._outcome(claim, succeeded=False, error_code="DEADLINE_EXCEEDED")
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

                guard.require_ownership()
                # Candidate upload precedes the fenced DB transaction. A DB error leaves the
                # attempt in RESULT_PERSISTING so the same logical attempt can be reclaimed.
                decision = self._succeed(claim, result, stored)
                succeeded = decision in {
                    PublicationDecision.PUBLISHED,
                    PublicationDecision.ALREADY_PUBLISHED,
                }
                publication_status = (
                    ResultPublicationStatus(decision.value) if stored is not None else None
                )
                return ExecutionOutcome(
                    task_id,
                    claim.attempt_id,
                    executed=True,
                    succeeded=succeeded,
                    publication_status=publication_status,
                    candidate_persisted=stored is not None,
                )
        except OwnershipLostError:
            return self._outcome(claim, succeeded=False)
        except TaskDeadlineExceededError:
            self._mark_deadline(claim)
            return self._outcome(claim, succeeded=False, error_code="DEADLINE_EXCEEDED")
        finally:
            guard.stop()

    @staticmethod
    def _outcome(
        claim: ExecutionClaim, *, succeeded: bool, error_code: str | None = None
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            claim.task_id,
            claim.attempt_id,
            executed=True,
            succeeded=succeeded,
            failure_code=error_code,
        )

    def _store_result(self, claim: ExecutionClaim, result: GenerationResult) -> StoredAsset | None:
        if self._asset_store is None:
            return None
        identity = result_identity(result.content, result.content_type)
        expected = StoredAsset(
            object_key=candidate_object_key(claim.task_id, claim.attempt_id, identity),
            content_type=identity.content_type,
            size_bytes=identity.size_bytes,
            sha256=identity.sha256,
            width=identity.width,
            height=identity.height,
        )
        stored = self._asset_store.put_result(
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            content=result.content,
            content_type=identity.content_type,
        )
        if stored != expected:
            raise RuntimeError("result asset store returned metadata for a different candidate")
        return stored

    def _set_phase(self, claim: ExecutionClaim, target: AttemptPhase, guard: LeaseGuard) -> None:
        guard.require_ownership()
        now = self._clock()
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if not self._claim_is_current(attempt, task, claim, now):
                guard.invalidate_ownership()
                raise OwnershipLostError("execution ownership was lost")
            assert attempt is not None and task is not None
            current = self._stored_phase(attempt.phase, attempt.status)
            if not phase_transition_allowed(current, target):
                raise ValueError(
                    f"attempt phase cannot move from {current.value} to {target.value}"
                )
            if current is target:
                return
            attempt.phase = target.value
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="ATTEMPT_PHASE_CHANGED",
                    payload={"attempt_id": str(claim.attempt_id), "phase": target.value},
                    created_at=now,
                )
            )

    def _record_result_storage_failure(self, claim: ExecutionClaim) -> bool:
        now = self._clock()
        error_code = "RESULT_STORAGE_ERROR"
        error_message = "result storage failed; the current attempt will be recovered"
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if (
                attempt is None
                or task is None
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
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
            if task.deadline_at <= now:
                self._mark_deadline_locked(session, task, now, latest)
                return None
            lease_settings, retry_policy = self._execution_policy(task.policy_snapshot)
            if latest is not None and latest.status == "SUCCEEDED":
                return None
            if latest is not None and latest.status == "RUNNING" and latest.lease_expires_at > now:
                return None
            if latest is not None and latest.status == "RUNNING":
                latest_phase = self._stored_phase(latest.phase, latest.status)
                has_remote_id = latest.provider_request_id is not None
                if (
                    next_phase_for_recovery(latest_phase, has_remote_request_id=has_remote_id)
                    is None
                ):
                    if submission_outcome_is_unknown(
                        latest_phase, has_remote_request_id=has_remote_id
                    ):
                        self._mark_submission_unknown_locked(session, task, latest, now)
                    return None

            if latest is None or latest.status == "FAILED":
                if latest is not None and latest.provider_request_id is not None:
                    return None
                sequence = 1 if latest is None else latest.sequence + 1
                attempt = GenerationAttemptModel(
                    id=uuid4(),
                    task_id=task_id,
                    sequence=sequence,
                    status="RUNNING",
                    phase=AttemptPhase.INPUT_LOADING.value,
                    provider_name=task.provider_name,
                    provider_request_key=f"{task_id}:attempt:{sequence}",
                    provider_request_id=None,
                    execution_token=self._token_factory(),
                    lease_expires_at=min(
                        now + timedelta(seconds=lease_settings.lease_seconds),
                        task.deadline_at,
                    ),
                    started_at=now,
                )
                session.add(attempt)
                event_type = "ATTEMPT_STARTED"
            else:
                attempt = latest
                sequence = attempt.sequence
                attempt.status = "RUNNING"
                attempt.execution_token = self._token_factory()
                attempt.lease_expires_at = min(
                    now + timedelta(seconds=lease_settings.lease_seconds), task.deadline_at
                )
                attempt.error_code = None
                attempt.error_message = None
                attempt.finished_at = None
                event_type = "ATTEMPT_RECLAIMED"

            task.status = TaskStatus.RUNNING.value
            task.next_attempt_at = None
            task.error_code = None
            task.error_message = None
            task.started_at = task.started_at or now
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=task_id,
                    event_type=event_type,
                    payload={
                        "attempt_id": str(attempt.id),
                        "sequence": sequence,
                        "phase": attempt.phase,
                    },
                    created_at=now,
                )
            )
            return ExecutionClaim(
                task_id=task_id,
                attempt_id=attempt.id,
                sequence=sequence,
                execution_token=attempt.execution_token,
                phase=self._stored_phase(attempt.phase, attempt.status),
                provider_request_key=attempt.provider_request_key,
                remote_request_id=attempt.provider_request_id,
                input=(
                    ImageToImageInput(
                        prompt=task.prompt,
                        size_preset=task.size_preset,
                        reference_asset_id=task.reference_asset_id,
                        reference_sha256=task.reference_sha256,
                    )
                    if GenerationType(task.generation_type) is GenerationType.IMAGE_TO_IMAGE
                    and task.reference_asset_id is not None
                    and task.reference_sha256 is not None
                    else self._text_or_invalid_input(task)
                ),
                deadline_at=task.deadline_at,
                lease_settings=lease_settings,
                retry_policy=retry_policy,
            )

    @staticmethod
    def _text_or_invalid_input(task: GenerationTaskModel) -> TaskInput:
        if GenerationType(task.generation_type) is not GenerationType.TEXT_TO_IMAGE:
            raise ValueError("image-to-image task is missing its frozen reference snapshot")
        return TextToImageInput(prompt=task.prompt, size_preset=task.size_preset)

    def _provider_request(
        self, claim: ExecutionClaim, guard: LeaseGuard
    ) -> ProviderGenerationRequest:
        if isinstance(claim.input, ImageToImageInput):
            if self._reference_reader is None:
                raise ReferenceAssetReadError("INPUT_REFERENCE_UNAVAILABLE", retryable=False)
            reference = self._reference_reader.read_for_task(
                claim.input.reference_asset_id,
                expected_sha256=claim.input.reference_sha256,
                ownership_guard=guard.require_ownership,
            )
            provider_input = ImageToImageProviderInput(
                prompt=claim.input.prompt,
                size_preset=claim.input.size_preset,
                reference_image=reference,
            )
        else:
            provider_input = TextToImageProviderInput(
                prompt=claim.input.prompt,
                size_preset=claim.input.size_preset,
            )
        return ProviderGenerationRequest(input=provider_input, deadline_at=claim.deadline_at)

    def _execution_policy(
        self, policy_snapshot: dict[str, object]
    ) -> tuple[LeaseSettings, RetryPolicy]:
        if policy_snapshot.get("frozen") is not True:
            return self._fallback_lease_settings, self._fallback_retry_policy

        lease_values = policy_snapshot.get("lease_settings")
        retry_values = policy_snapshot.get("retry_policy")
        if not isinstance(lease_values, dict) or not isinstance(retry_values, dict):
            raise ValueError("task policy snapshot is incomplete")
        lease_values = cast(dict[str, object], lease_values)
        retry_values = cast(dict[str, object], retry_values)
        try:
            lease_settings = LeaseSettings(
                lease_seconds=self._snapshot_number(lease_values, "lease_seconds"),
                heartbeat_interval_seconds=self._snapshot_number(
                    lease_values, "heartbeat_interval_seconds"
                ),
            )
            retry_policy = RetryPolicy(
                initial_delay_seconds=self._snapshot_number(retry_values, "initial_delay_seconds"),
                multiplier=self._snapshot_number(retry_values, "multiplier"),
                max_delay_seconds=self._snapshot_number(retry_values, "max_delay_seconds"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("task policy snapshot is invalid") from error
        if self._lease_settings_override is not None:
            lease_settings = self._lease_settings_override
        if self._retry_policy_override is not None:
            retry_policy = self._retry_policy_override
        return lease_settings, retry_policy

    @staticmethod
    def _snapshot_number(values: dict[str, object], key: str) -> float:
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"task policy snapshot field {key} is invalid")
        return float(value)

    @staticmethod
    def _stored_phase(phase: str, attempt_status: str) -> AttemptPhase:
        try:
            return AttemptPhase(phase)
        except ValueError:
            if attempt_status == "SUCCEEDED":
                return AttemptPhase.COMPLETED
            # MVP records used terminal phase labels; active pre-stage rows only used
            # PROVIDER_RUNNING and RESULT_PERSISTING, so unknown active values fail closed.
            raise ValueError(f"unsupported active attempt phase: {phase}") from None

    @staticmethod
    def _mark_submission_unknown_locked(
        session: Session,
        task: GenerationTaskModel,
        attempt: GenerationAttemptModel,
        now: datetime,
    ) -> None:
        code = "PROVIDER_SUBMISSION_UNKNOWN"
        message = (
            "provider may have accepted the creation request; automatic resubmission is disabled"
        )
        attempt.status = "FAILED"
        attempt.error_code = code
        attempt.error_message = message
        attempt.finished_at = now
        task.status = TaskStatus.FAILED.value
        task.error_code = code
        task.error_message = message
        task.completed_at = now
        task.next_attempt_at = None
        task.version += 1
        session.add(
            TaskEventModel(
                id=uuid4(),
                task_id=task.id,
                event_type="TASK_FAILED",
                payload={
                    "attempt_id": str(attempt.id),
                    "error_code": code,
                    "possible_external_call": True,
                },
                created_at=now,
            )
        )

    def _record_provider_request_id(
        self, claim: ExecutionClaim, provider_request_id: str, guard: LeaseGuard
    ) -> None:
        guard.require_ownership()
        now = self._clock()
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if not self._claim_is_current(attempt, task, claim, now):
                guard.invalidate_ownership()
                raise OwnershipLostError("execution ownership was lost")
            assert attempt is not None and task is not None
            if attempt.provider_request_id not in (None, provider_request_id):
                guard.invalidate_ownership()
                raise OwnershipLostError("a different remote request is already recorded")
            current = self._stored_phase(attempt.phase, attempt.status)
            if not phase_transition_allowed(current, AttemptPhase.PROVIDER_RUNNING):
                raise ValueError("remote request ID arrived in an invalid attempt phase")
            changed = attempt.provider_request_id is None or (
                current is not AttemptPhase.PROVIDER_RUNNING
            )
            attempt.provider_request_id = provider_request_id
            if current is not AttemptPhase.PROVIDER_RUNNING:
                attempt.phase = AttemptPhase.PROVIDER_RUNNING.value
            if changed:
                task.version += 1
                session.add(
                    TaskEventModel(
                        id=uuid4(),
                        task_id=claim.task_id,
                        event_type="ATTEMPT_PHASE_CHANGED",
                        payload={
                            "attempt_id": str(claim.attempt_id),
                            "phase": AttemptPhase.PROVIDER_RUNNING.value,
                            "remote_request_saved": True,
                        },
                        created_at=now,
                    )
                )

    def _succeed(
        self, claim: ExecutionClaim, result: GenerationResult, stored: StoredAsset | None
    ) -> PublicationDecision:
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.id == claim.attempt_id)
                .with_for_update()
            )
            if task is None or attempt is None:
                return PublicationDecision.OWNERSHIP_LOST

            # The task lock serializes publishers before the unique task/role constraint.
            existing_model = session.scalar(
                select(ResultAssetModel)
                .where(
                    ResultAssetModel.task_id == claim.task_id,
                    ResultAssetModel.role == "RESULT",
                )
                .with_for_update()
            )
            now = self._clock()
            if stored is None:
                if existing_model is not None:
                    return PublicationDecision.CONFLICTING_RESULT
                if not self._claim_is_current(attempt, task, claim, now):
                    return PublicationDecision.OWNERSHIP_LOST
                decision = PublicationDecision.PUBLISH
            else:
                candidate = ResultPointer(
                    attempt_id=claim.attempt_id,
                    object_key=stored.object_key,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                )
                existing = (
                    ResultPointer(
                        attempt_id=existing_model.attempt_id,
                        object_key=existing_model.object_key,
                        content_type=existing_model.content_type,
                        size_bytes=existing_model.size_bytes,
                        sha256=existing_model.sha256,
                    )
                    if existing_model is not None
                    else None
                )
                decision = decide_result_publication(
                    token_matches=attempt.execution_token == claim.execution_token,
                    lease_active=attempt.lease_expires_at > now,
                    deadline_active=task.deadline_at > now,
                    attempt_status=attempt.status,
                    task_status=task.status,
                    claim_attempt_id=claim.attempt_id,
                    existing=existing,
                    candidate=candidate,
                )
                if decision is PublicationDecision.ALREADY_PUBLISHED:
                    return decision
                if decision is not PublicationDecision.PUBLISH:
                    return decision
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
                        width=stored.width,
                        height=stored.height,
                        created_at=now,
                    )
                )
            attempt.status = "SUCCEEDED"
            attempt.phase = AttemptPhase.COMPLETED.value
            if result.provider_request_id is not None:
                attempt.provider_request_id = result.provider_request_id
            attempt.result_digest = stored.sha256 if stored is not None else result.result_digest
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
                    event_type="ATTEMPT_PHASE_CHANGED",
                    payload={
                        "attempt_id": str(claim.attempt_id),
                        "phase": AttemptPhase.COMPLETED.value,
                    },
                    created_at=now,
                )
            )
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="TASK_SUCCEEDED",
                    payload={
                        "attempt_id": str(claim.attempt_id),
                        "result_digest": attempt.result_digest,
                    },
                    created_at=now,
                )
            )
        return PublicationDecision.PUBLISHED

    def _handle_failure(
        self,
        claim: ExecutionClaim,
        guard: LeaseGuard,
        error: Exception,
        *,
        phase: AttemptPhase,
        remote_request_id: str | None,
    ) -> ExecutionOutcome:
        if isinstance(error, OwnershipLostError) or guard.state is LeaseState.OWNERSHIP_LOST:
            return self._outcome(claim, succeeded=False)
        if isinstance(error, TaskDeadlineExceededError) or (
            guard.state is LeaseState.DEADLINE_EXCEEDED
        ):
            self._mark_deadline(claim)
            return self._outcome(claim, succeeded=False, error_code="DEADLINE_EXCEEDED")

        if isinstance(error, ProviderError):
            code = error.code
            retryable = error.retryable
            submission_unknown = error.submission_state_unknown
            domain = classify_failure_domain(code, phase)
        elif isinstance(error, ReferenceAssetReadError):
            code = error.code
            retryable = error.retryable
            submission_unknown = False
            domain = FailureDomain.INPUT_LOADING
        elif phase is AttemptPhase.PROVIDER_SUBMITTING and remote_request_id is None:
            unknown = ProviderSubmissionUnknownError()
            code = unknown.code
            retryable = False
            submission_unknown = True
            domain = FailureDomain.PROVIDER_SUBMISSION
            error = unknown
        elif phase is AttemptPhase.RESULT_PERSISTING:
            code = "RESULT_STORAGE_ERROR"
            retryable = True
            submission_unknown = False
            domain = FailureDomain.RESULT_PERSISTENCE
        elif phase is AttemptPhase.RESULT_FETCHING and remote_request_id is not None:
            code = "RESULT_FETCH_UNAVAILABLE"
            retryable = True
            submission_unknown = False
            domain = FailureDomain.RESULT_FETCHING
        else:
            code = "EXECUTION_FAILED"
            retryable = False
            submission_unknown = False
            domain = FailureDomain.PLATFORM_CONFIGURATION

        action = decide_failure_action(
            domain=domain,
            retryable=retryable,
            phase=phase,
            has_remote_request_id=remote_request_id is not None,
            submission_state_unknown=submission_unknown,
        )
        if action is FailureAction.STOP:
            return self._outcome(claim, succeeded=False)
        if action is FailureAction.RETRY_SAME_ATTEMPT:
            self._record_same_attempt_recovery(claim, code, str(error))
            return self._outcome(claim, succeeded=False, error_code=code)

        self._fail(
            claim,
            error,
            code=code,
            retryable=retryable,
            allow_new_attempt=action is FailureAction.CREATE_NEW_ATTEMPT,
        )
        return self._outcome(claim, succeeded=False, error_code=code)

    def _record_same_attempt_recovery(
        self, claim: ExecutionClaim, error_code: str, error_message: str
    ) -> bool:
        now = self._clock()
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if not self._claim_is_current(attempt, task, claim, now):
                return False
            assert attempt is not None and task is not None
            attempt.error_code = error_code
            attempt.error_message = error_message
            task.error_code = error_code
            task.error_message = error_message
            task.version += 1
            session.add(
                TaskEventModel(
                    id=uuid4(),
                    task_id=claim.task_id,
                    event_type="ATTEMPT_RECOVERY_PENDING",
                    payload={"attempt_id": str(claim.attempt_id), "error_code": error_code},
                    created_at=now,
                )
            )
            return True

    def _fail(
        self,
        claim: ExecutionClaim,
        error: Exception,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        allow_new_attempt: bool = False,
    ) -> bool:
        now = self._clock()
        error_code, inferred_retryable = self._error_details(error)
        code = code or error_code
        retryable = inferred_retryable if retryable is None else retryable
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.scalar(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.id == claim.attempt_id)
                .with_for_update()
            )
            if (
                attempt is None
                or task is None
                or not self._claim_is_current(attempt, task, claim, now)
            ):
                return False
            attempt.status = "FAILED"
            attempt.error_code = code
            attempt.error_message = str(error)
            attempt.finished_at = now
            next_at = now
            if retryable:
                delay = claim.retry_policy.delay_seconds(claim.sequence, self._random_source())
                next_at = now + timedelta(seconds=delay)
            can_retry = can_schedule_new_attempt(
                action=(
                    FailureAction.CREATE_NEW_ATTEMPT if allow_new_attempt else FailureAction.FAIL
                ),
                attempts_used=claim.sequence,
                max_attempts=task.max_attempts,
                has_recoverable_remote_request=attempt.provider_request_id is not None,
                now=now,
                deadline_at=task.deadline_at,
                next_attempt_at=next_at,
            )
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
        return "EXECUTION_FAILED", False

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
            and task.status == TaskStatus.RUNNING.value
            and task.deadline_at > now
        )

    def _mark_deadline(self, claim: ExecutionClaim) -> bool:
        now = self._clock()
        with self._session_factory.begin() as session:
            task = session.get(GenerationTaskModel, claim.task_id, with_for_update=True)
            attempt = session.get(GenerationAttemptModel, claim.attempt_id, with_for_update=True)
            if (
                task is None
                or attempt is None
                or attempt.status != "RUNNING"
                or attempt.execution_token != claim.execution_token
                or task.status != TaskStatus.RUNNING.value
                or task.deadline_at > now
            ):
                return False
            self._mark_deadline_locked(session, task, now, attempt)
            return True

    @classmethod
    def _mark_deadline_locked(
        cls,
        session: Session,
        task: GenerationTaskModel,
        now: datetime,
        attempt: GenerationAttemptModel | None,
    ) -> None:
        unknown_submission = bool(
            attempt is not None
            and submission_outcome_is_unknown(
                cls._stored_phase(attempt.phase, attempt.status),
                has_remote_request_id=attempt.provider_request_id is not None,
            )
        )
        code = "PROVIDER_SUBMISSION_UNKNOWN" if unknown_submission else "DEADLINE_EXCEEDED"
        message = (
            "task deadline exceeded while provider submission outcome was unknown; "
            "the provider may have accepted it"
            if unknown_submission
            else "task deadline exceeded"
        )
        if attempt is not None and attempt.status == "RUNNING":
            attempt.status = "FAILED"
            attempt.error_code = code
            attempt.error_message = message
            attempt.finished_at = now
        task.status = TaskStatus.FAILED.value
        task.error_code = code
        task.error_message = message
        task.completed_at = now
        task.next_attempt_at = None
        task.version += 1
        session.add(
            TaskEventModel(
                id=uuid4(),
                task_id=task.id,
                event_type="TASK_FAILED",
                payload={
                    "attempt_id": str(attempt.id) if attempt is not None else None,
                    "error_code": code,
                    "deadline_exceeded": True,
                    "possible_external_call": unknown_submission,
                },
                created_at=now,
            )
        )
