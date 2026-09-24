from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from museflow.db.models import GenerationTaskModel, TaskEventModel
from museflow.db.session import create_session_factory
from museflow.tasks.execution_models import GenerationAttemptModel


@dataclass(frozen=True, slots=True)
class ReportTask:
    id: UUID
    status: str
    generation_type: str
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ReportAttempt:
    id: UUID
    task_id: UUID
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReportEvent:
    task_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a timezone, such as +00:00")
    return parsed.astimezone(UTC)


def _milliseconds(delta: timedelta) -> float:
    return round(delta.total_seconds() * 1000, 2)


def _summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "sample_count": len(ordered),
        "mean": round(statistics.fmean(ordered), 2),
        "median": round(statistics.median(ordered), 2),
        "p95": round(ordered[p95_index], 2),
        "max": round(ordered[-1], 2),
    }


def build_task_report(
    tasks: list[ReportTask],
    attempts: list[ReportAttempt],
    events: list[ReportEvent],
    *,
    since: datetime,
    until: datetime,
    window_task_count: int | None = None,
) -> dict[str, Any]:
    attempts_by_task: dict[UUID, list[ReportAttempt]] = defaultdict(list)
    attempts_by_id: dict[UUID, ReportAttempt] = {}
    for attempt in attempts:
        attempts_by_task[attempt.task_id].append(attempt)
        attempts_by_id[attempt.id] = attempt

    queue_latencies: list[float] = []
    end_to_end: list[float] = []
    attempt_durations: list[float] = []
    phase_durations: dict[str, list[float]] = defaultdict(list)
    by_status: Counter[str] = Counter()
    by_generation_type: Counter[str] = Counter()
    by_error_code: Counter[str] = Counter()
    task_rows: list[dict[str, Any]] = []
    automatic_retry_count = 0
    automatic_retry_succeeded = 0

    phase_starts: dict[UUID, list[tuple[datetime, str]]] = defaultdict(list)
    for event in sorted(events, key=lambda item: (item.created_at, str(item.task_id))):
        if event.event_type not in {
            "ATTEMPT_STARTED",
            "ATTEMPT_RECLAIMED",
            "ATTEMPT_PHASE_CHANGED",
        }:
            continue
        raw_attempt_id = event.payload.get("attempt_id")
        phase = event.payload.get("phase")
        if not isinstance(raw_attempt_id, str) or not isinstance(phase, str):
            continue
        try:
            attempt_id = UUID(raw_attempt_id)
        except ValueError:
            continue
        if attempt_id in attempts_by_id:
            phase_starts[attempt_id].append((event.created_at, phase))

    for attempt in attempts:
        if attempt.finished_at is not None and attempt.finished_at >= attempt.started_at:
            attempt_durations.append(_milliseconds(attempt.finished_at - attempt.started_at))
        entries = phase_starts.get(attempt.id, [])
        compact: list[tuple[datetime, str]] = []
        for entry in entries:
            if not compact or compact[-1][1] != entry[1]:
                compact.append(entry)
        end_time = attempt.finished_at
        for index, (started_at, phase) in enumerate(compact):
            next_at = compact[index + 1][0] if index + 1 < len(compact) else end_time
            if next_at is not None and next_at >= started_at:
                phase_durations[phase].append(_milliseconds(next_at - started_at))

    for task in tasks:
        task_attempts = sorted(
            attempts_by_task.get(task.id, []),
            key=lambda item: (item.started_at, str(item.id)),
        )
        by_status[task.status] += 1
        by_generation_type[task.generation_type] += 1
        by_error_code[task.error_code or "none"] += 1
        queue_latency = (
            _milliseconds(task.started_at - task.queued_at)
            if task.started_at is not None and task.started_at >= task.queued_at
            else None
        )
        e2e_latency = (
            _milliseconds(task.completed_at - task.queued_at)
            if task.completed_at is not None and task.completed_at >= task.queued_at
            else None
        )
        if queue_latency is not None:
            queue_latencies.append(queue_latency)
        if e2e_latency is not None:
            end_to_end.append(e2e_latency)
        if len(task_attempts) > 1:
            automatic_retry_count += 1
            if task.status == "SUCCEEDED":
                automatic_retry_succeeded += 1
        task_rows.append(
            {
                "task_id": str(task.id),
                "generation_type": task.generation_type,
                "status": task.status,
                "error_code": task.error_code,
                "attempt_count": len(task_attempts),
                "queue_latency_ms": queue_latency,
                "end_to_end_ms": e2e_latency,
            }
        )

    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "task_count": len(tasks),
        "window_task_count": window_task_count if window_task_count is not None else len(tasks),
        "truncated": (window_task_count or 0) > len(tasks),
        "by_generation_type": dict(sorted(by_generation_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_error_code": dict(sorted(by_error_code.items())),
        "queue_latency_ms": _summary(queue_latencies),
        "end_to_end_ms": _summary(end_to_end),
        "attempt_count": len(attempts),
        "attempt_duration_ms": _summary(attempt_durations),
        "phase_duration_ms": {
            phase: summary
            for phase, values in sorted(phase_durations.items())
            if (summary := _summary(values)) is not None
        },
        "automatic_retry_recovery": {
            "task_count": automatic_retry_count,
            "succeeded_task_count": automatic_retry_succeeded,
            "success_rate": (
                round(automatic_retry_succeeded / automatic_retry_count, 4)
                if automatic_retry_count
                else None
            ),
        },
        "tasks": task_rows,
    }


def collect_task_report(
    session: Session,
    *,
    since: datetime,
    until: datetime,
    limit: int,
) -> dict[str, Any]:
    filters = (
        GenerationTaskModel.queued_at >= since,
        GenerationTaskModel.queued_at < until,
    )
    total = session.scalar(
        select(func.count()).select_from(GenerationTaskModel).where(*filters)
    ) or 0
    task_models = list(
        session.scalars(
            select(GenerationTaskModel)
            .where(*filters)
            .order_by(GenerationTaskModel.queued_at, GenerationTaskModel.id)
            .limit(limit)
        )
    )
    task_ids = [task.id for task in task_models]
    attempt_models = (
        list(
            session.scalars(
                select(GenerationAttemptModel)
                .where(GenerationAttemptModel.task_id.in_(task_ids))
                .order_by(GenerationAttemptModel.started_at, GenerationAttemptModel.id)
            )
        )
        if task_ids
        else []
    )
    event_models = (
        list(
            session.scalars(
                select(TaskEventModel)
                .where(
                    TaskEventModel.task_id.in_(task_ids),
                    TaskEventModel.event_type.in_(
                        ("ATTEMPT_STARTED", "ATTEMPT_RECLAIMED", "ATTEMPT_PHASE_CHANGED")
                    ),
                )
                .order_by(TaskEventModel.created_at, TaskEventModel.id)
            )
        )
        if task_ids
        else []
    )
    return build_task_report(
        [
            ReportTask(
                id=task.id,
                status=task.status,
                generation_type=task.generation_type,
                queued_at=task.queued_at,
                started_at=task.started_at,
                completed_at=task.completed_at,
                error_code=task.error_code,
            )
            for task in task_models
        ],
        [
            ReportAttempt(
                id=attempt.id,
                task_id=attempt.task_id,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
            )
            for attempt in attempt_models
        ],
        [
            ReportEvent(
                task_id=event.task_id,
                event_type=event.event_type,
                payload=event.payload,
                created_at=event.created_at,
            )
            for event in event_models
        ],
        since=since,
        until=until,
        window_task_count=total,
    )


def main() -> None:
    now = datetime.now(UTC)
    parser = argparse.ArgumentParser(
        description="Print a read-only JSON task latency and retry report."
    )
    parser.add_argument("--since", type=parse_datetime, default=now - timedelta(hours=24))
    parser.add_argument("--until", type=parse_datetime, default=now)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if args.since >= args.until:
        parser.error("--since must be earlier than --until")
    if not 1 <= args.limit <= 10000:
        parser.error("--limit must be between 1 and 10000")
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    factory = create_session_factory(database_url)
    with factory() as session:
        report = collect_task_report(
            session,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
