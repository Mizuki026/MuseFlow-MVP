from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from museflow.safe_logging import log_task_event
from museflow.tasks.execution_semantics import LeaseSettings

logger = logging.getLogger(__name__)

_HEARTBEAT_SQL = text(
    """
    WITH task_lock AS (
        UPDATE generation_tasks AS task
        SET version = task.version
        WHERE task.id = :task_id
          AND task.status = 'RUNNING'
          AND task.deadline_at > :now
        RETURNING task.id, task.deadline_at
    )
    UPDATE generation_attempts AS attempt
    SET lease_expires_at = LEAST(
        GREATEST(attempt.lease_expires_at, :lease_until),
        task_lock.deadline_at
    )
    FROM task_lock
    WHERE attempt.id = :attempt_id
      AND attempt.task_id = task_lock.id
      AND attempt.execution_token = :execution_token
      AND attempt.status = 'RUNNING'
      AND attempt.lease_expires_at > :now
    RETURNING attempt.lease_expires_at
    """
)


class LeaseState(StrEnum):
    OWNED = "OWNED"
    OWNERSHIP_LOST = "OWNERSHIP_LOST"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class OwnershipLostError(RuntimeError):
    pass


class TaskDeadlineExceededError(RuntimeError):
    pass


class LeaseGuard:
    """Conditionally renew one execution claim and signal when it is no longer safe."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        task_id: UUID,
        attempt_id: UUID,
        execution_token: UUID,
        deadline_at: datetime,
        settings: LeaseSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if deadline_at.tzinfo is None:
            raise ValueError("task deadline must be timezone-aware")
        self._session_factory = session_factory
        self._task_id = task_id
        self._attempt_id = attempt_id
        self._execution_token = execution_token
        self._deadline_at = deadline_at
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = LeaseState.OWNED
        self._state_lock = threading.Lock()
        self._heartbeat_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> LeaseState:
        with self._state_lock:
            return self._state

    def __enter__(self) -> LeaseGuard:
        self.require_ownership()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self._task_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None

    def check(self) -> LeaseState:
        """Run a fenced heartbeat and return the stable ownership state."""
        if self.state is not LeaseState.OWNED:
            return self.state
        now = self._clock()
        if now >= self._deadline_at:
            self._set_state(LeaseState.DEADLINE_EXCEEDED)
            self._stop.set()
            return self.state
        try:
            with self._heartbeat_lock:
                if self.state is not LeaseState.OWNED:
                    return self.state
                with self._session_factory.begin() as session:
                    # The statement limit keeps shutdown bounded when PostgreSQL stalls.
                    session.execute(text("SET LOCAL statement_timeout = '5s'"))
                    renewed_until = session.execute(
                        _HEARTBEAT_SQL,
                        {
                            "task_id": self._task_id,
                            "attempt_id": self._attempt_id,
                            "execution_token": self._execution_token,
                            "now": now,
                            "lease_until": now + timedelta(seconds=self._settings.lease_seconds),
                        },
                    ).scalar_one_or_none()
            if renewed_until is None:
                new_state = (
                    LeaseState.DEADLINE_EXCEEDED
                    if self._clock() >= self._deadline_at
                    else LeaseState.OWNERSHIP_LOST
                )
                self._set_state(new_state)
                log_task_event(
                    logger,
                    "execution_ownership_lost",
                    level=logging.WARNING,
                    task_id=str(self._task_id),
                    attempt_id=str(self._attempt_id),
                    error_code=new_state.value,
                    status=new_state.value.lower(),
                )
                self._stop.set()
            return self.state
        except Exception as error:
            log_task_event(
                logger,
                "lease_heartbeat_failed",
                level=logging.ERROR,
                task_id=str(self._task_id),
                attempt_id=str(self._attempt_id),
                error_code="LEASE_HEARTBEAT_FAILED",
                error_type=type(error).__name__,
                status="ownership_lost",
            )
            self._set_state(LeaseState.OWNERSHIP_LOST)
            self._stop.set()
            return self.state

    def require_ownership(self) -> None:
        state = self.check()
        if state is LeaseState.OWNERSHIP_LOST:
            raise OwnershipLostError("execution ownership was lost")
        if state is LeaseState.DEADLINE_EXCEEDED:
            raise TaskDeadlineExceededError("task deadline exceeded")

    def invalidate_ownership(self) -> None:
        """Stop local work after a token-fenced write rejects this execution."""
        self._set_state(LeaseState.OWNERSHIP_LOST)
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            remaining = max(0.0, (self._deadline_at - now).total_seconds())
            wait_seconds = min(self._settings.heartbeat_interval_seconds, remaining)
            if self._stop.wait(wait_seconds):
                return
            if self._clock() >= self._deadline_at:
                self._set_state(LeaseState.DEADLINE_EXCEEDED)
                self._stop.set()
                return
            if self.check() is not LeaseState.OWNED:
                return

    def _set_state(self, state: LeaseState) -> None:
        with self._state_lock:
            if self._state is LeaseState.OWNED:
                self._state = state
