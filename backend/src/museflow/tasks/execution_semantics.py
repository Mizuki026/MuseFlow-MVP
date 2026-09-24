from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AttemptPhase(StrEnum):
    INPUT_LOADING = "INPUT_LOADING"
    PROVIDER_SUBMITTING = "PROVIDER_SUBMITTING"
    PROVIDER_RUNNING = "PROVIDER_RUNNING"
    RESULT_FETCHING = "RESULT_FETCHING"
    RESULT_PERSISTING = "RESULT_PERSISTING"
    COMPLETED = "COMPLETED"


class FailureDomain(StrEnum):
    INPUT_LOADING = "INPUT_LOADING"
    PLATFORM_CONFIGURATION = "PLATFORM_CONFIGURATION"
    OWNERSHIP = "OWNERSHIP"
    DEADLINE = "DEADLINE"
    PROVIDER_SUBMISSION = "PROVIDER_SUBMISSION"
    PROVIDER_EXECUTION = "PROVIDER_EXECUTION"
    RESULT_FETCHING = "RESULT_FETCHING"
    RESULT_VALIDATION = "RESULT_VALIDATION"
    RESULT_PERSISTENCE = "RESULT_PERSISTENCE"
    DATABASE = "DATABASE"


class FailureAction(StrEnum):
    STOP = "STOP"
    FAIL = "FAIL"
    RETRY_SAME_ATTEMPT = "RETRY_SAME_ATTEMPT"
    CREATE_NEW_ATTEMPT = "CREATE_NEW_ATTEMPT"


_PHASE_ORDER = tuple(AttemptPhase)


def phase_transition_allowed(current: AttemptPhase, target: AttemptPhase) -> bool:
    """Allow idempotent phase replay and one-step forward transitions only."""
    if current is target:
        return True
    if current is AttemptPhase.COMPLETED:
        return False
    return _PHASE_ORDER.index(target) == _PHASE_ORDER.index(current) + 1


def classify_failure_domain(error_code: str, phase: AttemptPhase) -> FailureDomain:
    if error_code in {
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_AUTHENTICATION",
        "PROVIDER_PROFILE_UNAVAILABLE",
    }:
        return FailureDomain.PLATFORM_CONFIGURATION
    if error_code.startswith("INPUT_"):
        return FailureDomain.INPUT_LOADING
    if error_code == "PROVIDER_SUBMISSION_UNKNOWN":
        return FailureDomain.PROVIDER_SUBMISSION
    if error_code == "RESULT_INVALID":
        return FailureDomain.RESULT_VALIDATION
    if error_code.startswith(("RESULT_DOWNLOAD", "RESULT_FETCH")):
        return FailureDomain.RESULT_FETCHING
    if error_code == "RESULT_STORAGE_ERROR":
        return FailureDomain.RESULT_PERSISTENCE
    if phase is AttemptPhase.INPUT_LOADING and error_code.startswith("PROVIDER_"):
        return FailureDomain.PROVIDER_SUBMISSION
    return {
        AttemptPhase.INPUT_LOADING: FailureDomain.INPUT_LOADING,
        AttemptPhase.PROVIDER_SUBMITTING: FailureDomain.PROVIDER_SUBMISSION,
        AttemptPhase.PROVIDER_RUNNING: FailureDomain.PROVIDER_EXECUTION,
        AttemptPhase.RESULT_FETCHING: FailureDomain.RESULT_FETCHING,
        AttemptPhase.RESULT_PERSISTING: FailureDomain.RESULT_PERSISTENCE,
        AttemptPhase.COMPLETED: FailureDomain.DATABASE,
    }[phase]


def next_phase_for_recovery(
    phase: AttemptPhase, *, has_remote_request_id: bool
) -> AttemptPhase | None:
    """Return the persisted phase a reclaimed worker must resume."""
    if phase is AttemptPhase.PROVIDER_SUBMITTING and not has_remote_request_id:
        return None
    if phase is AttemptPhase.COMPLETED:
        return None
    return phase


def submission_outcome_is_unknown(phase: AttemptPhase, *, has_remote_request_id: bool) -> bool:
    return not has_remote_request_id and phase in {
        AttemptPhase.PROVIDER_SUBMITTING,
        AttemptPhase.PROVIDER_RUNNING,
    }


def decide_failure_action(
    *,
    domain: FailureDomain,
    retryable: bool,
    phase: AttemptPhase,
    has_remote_request_id: bool,
    submission_state_unknown: bool = False,
) -> FailureAction:
    """Choose recovery without conflating an existing remote job with a new call."""
    if domain is FailureDomain.OWNERSHIP:
        return FailureAction.STOP
    if domain is FailureDomain.DEADLINE:
        return FailureAction.FAIL
    if submission_state_unknown:
        return FailureAction.FAIL
    if phase is AttemptPhase.INPUT_LOADING and domain is FailureDomain.INPUT_LOADING:
        return (
            FailureAction.RETRY_SAME_ATTEMPT
            if retryable and domain is FailureDomain.INPUT_LOADING
            else FailureAction.FAIL
        )
    if phase in {AttemptPhase.INPUT_LOADING, AttemptPhase.PROVIDER_SUBMITTING} and (
        not has_remote_request_id
    ):
        if retryable and domain is FailureDomain.PROVIDER_SUBMISSION:
            return FailureAction.CREATE_NEW_ATTEMPT
        return FailureAction.FAIL
    if has_remote_request_id and retryable:
        return FailureAction.RETRY_SAME_ATTEMPT
    if retryable:
        return FailureAction.CREATE_NEW_ATTEMPT
    return FailureAction.FAIL


def can_schedule_new_attempt(
    *,
    action: FailureAction,
    attempts_used: int,
    max_attempts: int,
    has_recoverable_remote_request: bool,
    now: datetime,
    deadline_at: datetime,
    next_attempt_at: datetime,
) -> bool:
    return bool(
        action is FailureAction.CREATE_NEW_ATTEMPT
        and not has_recoverable_remote_request
        and attempts_used < max_attempts
        and now < deadline_at
        and next_attempt_at < deadline_at
    )


@dataclass(frozen=True, slots=True)
class LeaseSettings:
    lease_seconds: float = 240.0
    heartbeat_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0 or self.heartbeat_interval_seconds <= 0:
            raise ValueError("lease and heartbeat intervals must be positive")
        if self.heartbeat_interval_seconds * 3 > self.lease_seconds:
            raise ValueError("heartbeat interval must not exceed one third of the lease")

    @classmethod
    def from_environment(cls) -> LeaseSettings:
        lease_seconds = float(os.environ.get("MUSEFLOW_LEASE_SECONDS", "240"))
        heartbeat_interval_seconds = float(
            os.environ.get("MUSEFLOW_HEARTBEAT_INTERVAL_SECONDS", "30")
        )
        return cls(
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
