from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from museflow.task_report import (
    ReportAttempt,
    ReportEvent,
    ReportTask,
    build_task_report,
    parse_datetime,
)


def test_parse_datetime_requires_timezone_and_normalizes_utc() -> None:
    parsed = parse_datetime("2026-09-24T08:00:00+08:00")
    assert parsed == datetime(2026, 9, 24, 0, 0, tzinfo=UTC)


def test_task_report_calculates_phase_timings_and_retry_recovery_without_private_fields() -> None:
    start = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    task_id = UUID("00000000-0000-0000-0000-000000000001")
    attempt_one = UUID("00000000-0000-0000-0000-000000000011")
    attempt_two = UUID("00000000-0000-0000-0000-000000000012")
    report = build_task_report(
        [
            ReportTask(
                id=task_id,
                status="SUCCEEDED",
                generation_type="IMAGE_TO_IMAGE",
                queued_at=start,
                started_at=start + timedelta(seconds=1),
                completed_at=start + timedelta(seconds=10),
                error_code=None,
            )
        ],
        [
            ReportAttempt(
                id=attempt_one,
                task_id=task_id,
                started_at=start + timedelta(seconds=1),
                finished_at=start + timedelta(seconds=4),
            ),
            ReportAttempt(
                id=attempt_two,
                task_id=task_id,
                started_at=start + timedelta(seconds=5),
                finished_at=start + timedelta(seconds=10),
            ),
        ],
        [
            ReportEvent(
                task_id,
                "ATTEMPT_STARTED",
                {"attempt_id": str(attempt_one), "phase": "INPUT_LOADING"},
                start + timedelta(seconds=1),
            ),
            ReportEvent(
                task_id,
                "ATTEMPT_PHASE_CHANGED",
                {"attempt_id": str(attempt_one), "phase": "PROVIDER_SUBMITTING"},
                start + timedelta(seconds=2),
            ),
            ReportEvent(
                task_id,
                "ATTEMPT_PHASE_CHANGED",
                {"attempt_id": str(attempt_one), "phase": "PROVIDER_RUNNING"},
                start + timedelta(seconds=3),
            ),
            ReportEvent(
                task_id,
                "ATTEMPT_STARTED",
                {"attempt_id": str(attempt_two), "phase": "INPUT_LOADING"},
                start + timedelta(seconds=5),
            ),
            ReportEvent(
                task_id,
                "ATTEMPT_PHASE_CHANGED",
                {"attempt_id": str(attempt_two), "phase": "PROVIDER_RUNNING"},
                start + timedelta(seconds=7),
            ),
            ReportEvent(
                task_id,
                "ATTEMPT_PHASE_CHANGED",
                {"attempt_id": str(attempt_two), "phase": "COMPLETED"},
                start + timedelta(seconds=10),
            ),
        ],
        since=start,
        until=start + timedelta(days=1),
    )

    assert report["queue_latency_ms"] == {
        "sample_count": 1,
        "mean": 1000.0,
        "median": 1000.0,
        "p95": 1000.0,
        "max": 1000.0,
    }
    assert report["end_to_end_ms"]["mean"] == 10000.0
    assert report["attempt_duration_ms"]["sample_count"] == 2
    assert report["phase_duration_ms"]["PROVIDER_SUBMITTING"]["mean"] == 1000.0
    assert report["phase_duration_ms"]["PROVIDER_RUNNING"]["sample_count"] == 2
    assert report["automatic_retry_recovery"] == {
        "task_count": 1,
        "succeeded_task_count": 1,
        "success_rate": 1.0,
    }
    rendered = str(report)
    for forbidden in ("prompt", "remote_request_id", "object_key", "signed_url"):
        assert forbidden not in rendered


def test_empty_task_report_has_empty_sample_values() -> None:
    now = datetime.now(UTC)
    report = build_task_report([], [], [], since=now - timedelta(hours=1), until=now)

    assert report["task_count"] == 0
    assert report["queue_latency_ms"] is None
    assert report["end_to_end_ms"] is None
    assert report["automatic_retry_recovery"]["success_rate"] is None
