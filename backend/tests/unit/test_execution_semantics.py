from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from museflow.tasks.domain import RetryPolicy
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


def test_attempt_phase_advances_one_step_and_allows_idempotent_replay() -> None:
    assert phase_transition_allowed(AttemptPhase.INPUT_LOADING, AttemptPhase.INPUT_LOADING)
    assert phase_transition_allowed(
        AttemptPhase.INPUT_LOADING, AttemptPhase.PROVIDER_SUBMITTING
    )
    assert not phase_transition_allowed(
        AttemptPhase.INPUT_LOADING, AttemptPhase.PROVIDER_RUNNING
    )
    assert not phase_transition_allowed(AttemptPhase.COMPLETED, AttemptPhase.RESULT_PERSISTING)
    assert not phase_transition_allowed(
        AttemptPhase.RESULT_PERSISTING, AttemptPhase.RESULT_FETCHING
    )


def test_recovery_resumes_persisted_phase_and_unknown_submission_has_no_resume_phase() -> None:
    assert (
        next_phase_for_recovery(
            AttemptPhase.RESULT_FETCHING,
            has_remote_request_id=True,
        )
        is AttemptPhase.RESULT_FETCHING
    )
    assert (
        next_phase_for_recovery(
            AttemptPhase.PROVIDER_SUBMITTING,
            has_remote_request_id=False,
        )
        is None
    )
    assert (
        next_phase_for_recovery(AttemptPhase.COMPLETED, has_remote_request_id=True) is None
    )
    assert submission_outcome_is_unknown(
        AttemptPhase.PROVIDER_RUNNING, has_remote_request_id=False
    )
    assert not submission_outcome_is_unknown(
        AttemptPhase.RESULT_FETCHING, has_remote_request_id=False
    )


@pytest.mark.parametrize(
    ("domain", "retryable", "phase", "remote_id", "unknown", "expected"),
    [
        (
            FailureDomain.OWNERSHIP,
            True,
            AttemptPhase.PROVIDER_RUNNING,
            False,
            False,
            FailureAction.STOP,
        ),
        (
            FailureDomain.PROVIDER_SUBMISSION,
            False,
            AttemptPhase.PROVIDER_SUBMITTING,
            False,
            True,
            FailureAction.FAIL,
        ),
        (
            FailureDomain.PROVIDER_SUBMISSION,
            True,
            AttemptPhase.PROVIDER_SUBMITTING,
            False,
            False,
            FailureAction.CREATE_NEW_ATTEMPT,
        ),
        (
            FailureDomain.PROVIDER_EXECUTION,
            True,
            AttemptPhase.PROVIDER_RUNNING,
            True,
            False,
            FailureAction.RETRY_SAME_ATTEMPT,
        ),
    ],
)
def test_failure_action_preserves_attempt_and_submission_boundaries(
    domain: FailureDomain,
    retryable: bool,
    phase: AttemptPhase,
    remote_id: bool,
    unknown: bool,
    expected: FailureAction,
) -> None:
    assert (
        decide_failure_action(
            domain=domain,
            retryable=retryable,
            phase=phase,
            has_remote_request_id=remote_id,
            submission_state_unknown=unknown,
        )
        is expected
    )


def test_error_domain_classification_is_stable_and_phase_aware() -> None:
    assert (
        classify_failure_domain("PROVIDER_NOT_CONFIGURED", AttemptPhase.INPUT_LOADING)
        is FailureDomain.PLATFORM_CONFIGURATION
    )
    assert (
        classify_failure_domain("PROVIDER_SUBMISSION_UNKNOWN", AttemptPhase.PROVIDER_SUBMITTING)
        is FailureDomain.PROVIDER_SUBMISSION
    )
    assert (
        classify_failure_domain("RESULT_DOWNLOAD_UNAVAILABLE", AttemptPhase.RESULT_FETCHING)
        is FailureDomain.RESULT_FETCHING
    )
    assert (
        classify_failure_domain("RESULT_STORAGE_ERROR", AttemptPhase.RESULT_PERSISTING)
        is FailureDomain.RESULT_PERSISTENCE
    )


def test_attempt_limit_deadline_and_remote_request_gate_new_attempts() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    deadline = now + timedelta(seconds=60)
    args = {
        "action": FailureAction.CREATE_NEW_ATTEMPT,
        "attempts_used": 1,
        "max_attempts": 3,
        "has_recoverable_remote_request": False,
        "now": now,
        "deadline_at": deadline,
        "next_attempt_at": now + timedelta(seconds=5),
    }
    assert can_schedule_new_attempt(**args)
    assert not can_schedule_new_attempt(**{**args, "attempts_used": 3})
    assert not can_schedule_new_attempt(**{**args, "has_recoverable_remote_request": True})
    assert not can_schedule_new_attempt(
        **{**args, "next_attempt_at": deadline}
    )
    assert not can_schedule_new_attempt(**{**args, "action": FailureAction.RETRY_SAME_ATTEMPT})


def test_retry_backoff_and_lease_configuration_are_bounded(monkeypatch) -> None:
    policy = RetryPolicy()
    assert policy.delay_seconds(1, 0.5) == 1
    assert policy.delay_seconds(2, 0.5) == 2
    assert policy.delay_seconds(5, 1) == policy.max_delay_seconds
    assert LeaseSettings(lease_seconds=90, heartbeat_interval_seconds=30)
    with pytest.raises(ValueError, match="one third"):
        LeaseSettings(lease_seconds=30, heartbeat_interval_seconds=11)
    monkeypatch.setenv("MUSEFLOW_LEASE_SECONDS", "60")
    monkeypatch.setenv("MUSEFLOW_HEARTBEAT_INTERVAL_SECONDS", "30")
    with pytest.raises(ValueError, match="one third"):
        LeaseSettings.from_environment()
