from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from museflow.tasks.execution_semantics import LeaseSettings
from museflow.tasks.lease_guard import (
    LeaseGuard,
    LeaseState,
    OwnershipLostError,
    TaskDeadlineExceededError,
)


def _guard(factory, *, now: datetime, deadline_at: datetime) -> LeaseGuard:
    return LeaseGuard(
        factory,
        task_id=uuid4(),
        attempt_id=uuid4(),
        execution_token=uuid4(),
        deadline_at=deadline_at,
        settings=LeaseSettings(lease_seconds=90, heartbeat_interval_seconds=20),
        clock=lambda: now,
    )


def test_deadline_expires_without_attempting_a_database_heartbeat() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    factory = MagicMock()
    guard = _guard(factory, now=now, deadline_at=now)

    assert guard.check() is LeaseState.DEADLINE_EXCEEDED
    factory.begin.assert_not_called()
    with pytest.raises(TaskDeadlineExceededError):
        guard.require_ownership()


def test_conditional_heartbeat_with_no_matching_claim_loses_ownership() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    factory = MagicMock()
    session = factory.begin.return_value.__enter__.return_value
    session.execute.return_value.scalar_one_or_none.return_value = None
    guard = _guard(factory, now=now, deadline_at=now + timedelta(minutes=5))

    assert guard.check() is LeaseState.OWNERSHIP_LOST
    with pytest.raises(OwnershipLostError):
        guard.require_ownership()


def test_database_error_fails_closed_as_lost_ownership() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    factory = MagicMock()
    session = factory.begin.return_value.__enter__.return_value
    session.execute.side_effect = RuntimeError("database unavailable")
    guard = _guard(factory, now=now, deadline_at=now + timedelta(minutes=5))

    assert guard.check() is LeaseState.OWNERSHIP_LOST
