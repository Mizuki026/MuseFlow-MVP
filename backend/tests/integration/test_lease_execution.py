from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from threading import enumerate as active_threads
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select

from museflow.db.models import (
    GenerationTaskModel,
    OutboxMessageModel,
    ResultAssetModel,
    TaskEventModel,
)
from museflow.db.session import create_session_factory
from museflow.providers import (
    GenerationRequest,
    GenerationResult,
    LeaseChecker,
    MockProvider,
    TransientProviderError,
)
from museflow.tasks.application import CreateTask
from museflow.tasks.domain import CreateTaskRequest, TaskPolicy, TaskStatus
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.execution_models import GenerationAttemptModel
from museflow.tasks.execution_semantics import AttemptPhase, LeaseSettings
from museflow.tasks.lease_guard import LeaseGuard, LeaseState, OwnershipLostError
from museflow.tasks.recovery import RecoverExpiredLeases


@pytest.fixture()
def session_factory():
    database_url = os.environ.get("MUSEFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MUSEFLOW_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_session_factory(database_url)


def _create_task(factory, *, deadline_seconds: int = 600) -> UUID:
    return CreateTask(
        factory,
        policy=TaskPolicy(
            max_attempts=3,
            policy_version="lease-tests",
            deadline_seconds=deadline_seconds,
        ),
    ).execute(CreateTaskRequest(prompt="lease execution test"), f"lease-{uuid4()}").task.id


def _cleanup(factory, task_id: UUID) -> None:
    with factory.begin() as session:
        session.execute(
            delete(OutboxMessageModel).where(OutboxMessageModel.aggregate_id == task_id)
        )
        session.execute(delete(TaskEventModel).where(TaskEventModel.task_id == task_id))
        session.execute(delete(ResultAssetModel).where(ResultAssetModel.task_id == task_id))
        session.execute(
            delete(GenerationAttemptModel).where(GenerationAttemptModel.task_id == task_id)
        )
        session.execute(delete(GenerationTaskModel).where(GenerationTaskModel.id == task_id))


def test_heartbeat_keeps_long_provider_execution_owned_while_scheduler_scans(
    session_factory,
) -> None:
    task_id = _create_task(session_factory)
    provider = MockProvider(execution_delay_seconds=1.3)
    executor = ExecuteGenerationAttempt(
        session_factory,
        provider,
        lease_settings=LeaseSettings(lease_seconds=0.6, heartbeat_interval_seconds=0.15),
    )
    scheduler_done = Event()
    reclaimed: list[int] = []

    def scan_expired_leases() -> None:
        while not scheduler_done.is_set():
            reclaimed.append(RecoverExpiredLeases(session_factory).run_once())
            time.sleep(0.04)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(executor.execute, task_id)
            with session_factory() as session:
                # Wait until the provider request has been submitted and the attempt is visible.
                deadline = time.monotonic() + 5
                attempt = None
                while time.monotonic() < deadline and attempt is None:
                    attempt = session.scalar(
                        select(GenerationAttemptModel).where(
                            GenerationAttemptModel.task_id == task_id
                        )
                    )
                    if attempt is None:
                        session.expire_all()
                        time.sleep(0.02)
            scanner = pool.submit(scan_expired_leases)
            outcome = worker.result(timeout=8)
            scheduler_done.set()
            scanner.result(timeout=3)

        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
        assert outcome.succeeded is True
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert len(attempts) == 1
        assert attempts[0].sequence == 1
        assert attempts[0].phase == AttemptPhase.COMPLETED.value
        assert attempts[0].lease_expires_at > attempts[0].started_at + timedelta(seconds=1)
        assert sum(reclaimed) == 0
        assert provider.create_calls == 1
    finally:
        scheduler_done.set()
        _cleanup(session_factory, task_id)


def test_heartbeat_is_fenced_by_token_and_task_status(session_factory) -> None:
    task_id = _create_task(session_factory)
    settings = LeaseSettings(lease_seconds=30, heartbeat_interval_seconds=5)
    executor = ExecuteGenerationAttempt(
        session_factory, MockProvider(), lease_settings=settings
    )
    try:
        claim = executor._claim(task_id)
        assert claim is not None
        with session_factory() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id)
            assert attempt is not None
            original_lease = attempt.lease_expires_at
            deadline = session.get(GenerationTaskModel, task_id).deadline_at
        wrong_token = LeaseGuard(
            session_factory,
            task_id=task_id,
            attempt_id=claim.attempt_id,
            execution_token=uuid4(),
            deadline_at=deadline,
            settings=settings,
        )
        assert wrong_token.check() is LeaseState.OWNERSHIP_LOST
        with session_factory() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id)
            assert attempt is not None and attempt.lease_expires_at == original_lease

        current_owner = LeaseGuard(
            session_factory,
            task_id=task_id,
            attempt_id=claim.attempt_id,
            execution_token=claim.execution_token,
            deadline_at=deadline,
            settings=settings,
        )
        assert current_owner.check() is LeaseState.OWNED
        with session_factory() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id)
            assert attempt is not None and attempt.lease_expires_at > original_lease
            renewed_lease = attempt.lease_expires_at

        with session_factory.begin() as session:
            task = session.get(GenerationTaskModel, task_id)
            assert task is not None
            task.status = TaskStatus.FAILED.value
        terminal_owner = LeaseGuard(
            session_factory,
            task_id=task_id,
            attempt_id=claim.attempt_id,
            execution_token=claim.execution_token,
            deadline_at=deadline,
            settings=settings,
        )
        assert terminal_owner.check() is LeaseState.OWNERSHIP_LOST
        with session_factory() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id)
            assert attempt is not None and attempt.lease_expires_at == renewed_lease
    finally:
        _cleanup(session_factory, task_id)


def test_database_heartbeat_failure_fails_closed_and_stops_thread(session_factory) -> None:
    task_id = _create_task(session_factory)
    claim = ExecuteGenerationAttempt(session_factory, MockProvider())._claim(task_id)
    assert claim is not None
    with session_factory() as session:
        task = session.get(GenerationTaskModel, task_id)
        assert task is not None
        deadline = task.deadline_at
    guard = LeaseGuard(
        session_factory,
        task_id=task_id,
        attempt_id=claim.attempt_id,
        execution_token=claim.execution_token,
        deadline_at=deadline,
        settings=LeaseSettings(lease_seconds=1, heartbeat_interval_seconds=0.2),
    )
    engine = session_factory.kw["bind"]

    def fail_heartbeat(*args, **kwargs) -> None:
        raise RuntimeError("simulated PostgreSQL outage")

    try:
        with guard:
            event.listen(engine, "before_cursor_execute", fail_heartbeat)
            end = time.monotonic() + 2
            while guard.state is LeaseState.OWNED and time.monotonic() < end:
                time.sleep(0.02)
            assert guard.state is LeaseState.OWNERSHIP_LOST
            with pytest.raises(OwnershipLostError):
                guard.require_ownership()
    finally:
        event.remove(engine, "before_cursor_execute", fail_heartbeat)
        guard.stop()
        _cleanup(session_factory, task_id)
    assert not any(thread.name == f"lease-heartbeat-{task_id}" for thread in active_threads())


def test_deadline_caps_heartbeat_and_fails_the_active_attempt(session_factory) -> None:
    task_id = _create_task(session_factory, deadline_seconds=1)
    provider = MockProvider(execution_delay_seconds=1.2)
    executor = ExecuteGenerationAttempt(
        session_factory,
        provider,
        lease_settings=LeaseSettings(lease_seconds=5, heartbeat_interval_seconds=0.5),
    )
    try:
        outcome = executor.execute(task_id)
        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt = session.scalar(
                select(GenerationAttemptModel).where(
                    GenerationAttemptModel.task_id == task_id
                )
            )
        assert outcome.failure_code == "DEADLINE_EXCEEDED"
        assert task is not None and task.status == TaskStatus.FAILED.value
        assert task.error_code == "DEADLINE_EXCEEDED"
        assert attempt is not None and attempt.status == "FAILED"
        assert attempt.phase == AttemptPhase.PROVIDER_RUNNING.value
        assert attempt.lease_expires_at <= task.deadline_at
        assert provider.create_calls == 1
    finally:
        _cleanup(session_factory, task_id)


@pytest.mark.parametrize(
    ("failure_kind", "expected_phase", "expected_fetches"),
    [
        ("poll", AttemptPhase.PROVIDER_RUNNING.value, 1),
        ("fetch", AttemptPhase.RESULT_FETCHING.value, 2),
    ],
)
def test_remote_request_recovery_keeps_one_attempt_and_never_resubmits(
    session_factory, failure_kind: str, expected_phase: str, expected_fetches: int
) -> None:
    task_id = _create_task(session_factory)
    provider = MockProvider(
        poll_failures=[TransientProviderError()] if failure_kind == "poll" else [],
        result_fetch_failures=[TransientProviderError()] if failure_kind == "fetch" else [],
    )
    executor = ExecuteGenerationAttempt(session_factory, provider, lease_seconds=1)
    try:
        first = executor.execute(task_id)
        assert first.succeeded is False
        with session_factory.begin() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel).where(
                    GenerationAttemptModel.task_id == task_id
                )
            )
            assert attempt is not None
            assert attempt.sequence == 1
            assert attempt.phase == expected_phase
            assert attempt.provider_request_id is not None
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert RecoverExpiredLeases(session_factory).run_once() == 1

        resumed = executor.execute(task_id)
        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
        assert resumed.succeeded is True
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert len(attempts) == 1
        assert attempts[0].phase == AttemptPhase.COMPLETED.value
        assert provider.create_calls == 1
        assert provider.recovery_calls == 1
        assert provider.poll_calls == 2
        assert provider.result_fetch_calls == expected_fetches
    finally:
        _cleanup(session_factory, task_id)


def test_unknown_provider_submission_is_terminal_and_never_resent(session_factory) -> None:
    task_id = _create_task(session_factory)
    provider = MockProvider(submission_unknown_once=True)
    executor = ExecuteGenerationAttempt(session_factory, provider)
    try:
        outcome = executor.execute(task_id)
        replay = executor.execute(task_id)
        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
        assert outcome.failure_code == "PROVIDER_SUBMISSION_UNKNOWN"
        assert replay.executed is False
        assert task is not None and task.status == TaskStatus.FAILED.value
        assert task.error_code == "PROVIDER_SUBMISSION_UNKNOWN"
        assert len(attempts) == 1
        assert attempts[0].phase == AttemptPhase.PROVIDER_SUBMITTING.value
        assert attempts[0].provider_request_id is None
        assert provider.create_calls == 1
    finally:
        _cleanup(session_factory, task_id)


@pytest.mark.parametrize(
    "phase",
    [AttemptPhase.PROVIDER_SUBMITTING.value, AttemptPhase.PROVIDER_RUNNING.value],
)
def test_recovered_unknown_submission_phase_without_remote_id_is_never_resubmitted(
    session_factory, phase: str
) -> None:
    task_id = _create_task(session_factory)
    provider = MockProvider()
    executor = ExecuteGenerationAttempt(session_factory, provider, lease_seconds=1)
    try:
        claim = executor._claim(task_id)
        assert claim is not None
        with session_factory.begin() as session:
            attempt = session.get(GenerationAttemptModel, claim.attempt_id)
            assert attempt is not None
            attempt.phase = phase
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert RecoverExpiredLeases(session_factory).run_once() == 1
        replay = executor.execute(task_id)
        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel).where(
                        GenerationAttemptModel.task_id == task_id
                    )
                )
            )
        assert replay.executed is False
        assert task is not None and task.error_code == "PROVIDER_SUBMISSION_UNKNOWN"
        assert len(attempts) == 1
        assert attempts[0].phase == phase
        assert provider.create_calls == 0
    finally:
        _cleanup(session_factory, task_id)


class BlockingProvider:
    name = "blocking-test"

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id=None,
        on_phase=None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult:
        del request
        assert remote_request_id is None
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_SUBMITTING)
        if on_remote_request_id is not None:
            on_remote_request_id(f"blocking-{request_key}")
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_RUNNING)
        self.started.set()
        assert self.release.wait(timeout=5)
        if lease_guard is not None:
            lease_guard.require_ownership()
        raise AssertionError("a fenced worker must stop before requesting a result")


class LateResponseProvider:
    name = "late-response-test"

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
        on_remote_request_id=None,
        on_phase=None,
        lease_guard: LeaseChecker | None = None,
    ) -> GenerationResult:
        del request, remote_request_id
        if on_phase is not None:
            on_phase(AttemptPhase.PROVIDER_SUBMITTING)
        self.started.set()
        assert self.release.wait(timeout=5)
        assert on_remote_request_id is not None
        on_remote_request_id(f"late-{request_key}")
        raise AssertionError("a late provider response must not proceed to polling")


def test_late_create_response_cannot_persist_remote_id_after_token_takeover(
    session_factory,
) -> None:
    task_id = _create_task(session_factory)
    provider = LateResponseProvider()
    executor = ExecuteGenerationAttempt(
        session_factory,
        provider,
        lease_settings=LeaseSettings(lease_seconds=3, heartbeat_interval_seconds=0.3),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(executor.execute, task_id)
            assert provider.started.wait(timeout=5)
            with session_factory.begin() as session:
                attempt = session.scalar(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task_id)
                    .with_for_update()
                )
                assert attempt is not None
                attempt.execution_token = uuid4()
            time.sleep(0.5)
            provider.release.set()
            outcome = future.result(timeout=5)

        with session_factory() as session:
            attempt = session.scalar(
                select(GenerationAttemptModel).where(
                    GenerationAttemptModel.task_id == task_id
                )
            )
            results = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
        assert outcome.succeeded is False
        assert attempt is not None
        assert attempt.phase == AttemptPhase.PROVIDER_SUBMITTING.value
        assert attempt.provider_request_id is None
        assert results == []
    finally:
        provider.release.set()
        _cleanup(session_factory, task_id)


def test_token_takeover_stops_old_worker_before_result_persistence(session_factory) -> None:
    task_id = _create_task(session_factory)
    provider = BlockingProvider()
    executor = ExecuteGenerationAttempt(
        session_factory,
        provider,
        lease_settings=LeaseSettings(lease_seconds=3, heartbeat_interval_seconds=0.3),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(executor.execute, task_id)
            assert provider.started.wait(timeout=5)
            with session_factory.begin() as session:
                attempt = session.scalar(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task_id)
                    .with_for_update()
                )
                assert attempt is not None
                attempt.execution_token = uuid4()
            time.sleep(0.5)
            provider.release.set()
            outcome = future.result(timeout=5)

        with session_factory() as session:
            task = session.get(GenerationTaskModel, task_id)
            attempt = session.scalar(
                select(GenerationAttemptModel).where(
                    GenerationAttemptModel.task_id == task_id
                )
            )
            results = list(
                session.scalars(
                    select(ResultAssetModel).where(ResultAssetModel.task_id == task_id)
                )
            )
        assert outcome.succeeded is False
        assert outcome.candidate_persisted is False
        assert task is not None and task.status == TaskStatus.RUNNING.value
        assert attempt is not None and attempt.phase == AttemptPhase.PROVIDER_RUNNING.value
        assert attempt.status == "RUNNING"
        assert results == []
    finally:
        provider.release.set()
        _cleanup(session_factory, task_id)


def test_worker_exit_stops_heartbeat_thread(session_factory) -> None:
    task_id = _create_task(session_factory)
    try:
        outcome = ExecuteGenerationAttempt(
            session_factory,
            MockProvider(),
            lease_settings=LeaseSettings(lease_seconds=3, heartbeat_interval_seconds=0.3),
        ).execute(task_id)
        assert outcome.succeeded is True
        assert not any(
            thread.name == f"lease-heartbeat-{task_id}" for thread in active_threads()
        )
    finally:
        _cleanup(session_factory, task_id)
