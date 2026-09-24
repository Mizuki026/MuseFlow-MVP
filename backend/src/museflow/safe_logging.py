from __future__ import annotations

import hashlib
import logging


def log_task_event(
    logger: logging.Logger,
    event_name: str,
    *,
    level: int = logging.INFO,
    task_id: str | None = None,
    asset_id: str | None = None,
    attempt_id: str | None = None,
    generation_type: str | None = None,
    provider_name: str | None = None,
    provider_profile: str | None = None,
    phase: str | None = None,
    error_code: str | None = None,
    duration_ms: float | None = None,
    remote_request_id: str | None = None,
    recovery: bool = False,
    maintenance_task: bool = False,
    status: str | None = None,
    error_type: str | None = None,
) -> None:
    """Emit a fixed-shape event without accepting prompts, URLs, or response bodies."""
    remote_digest = None
    if remote_request_id is not None:
        remote_digest = hashlib.sha256(remote_request_id.encode("utf-8")).hexdigest()[:12]
    logger.log(
        level,
        "museflow_event",
        extra={
            "event_name": event_name,
            "task_id": task_id,
            "asset_id": asset_id,
            "attempt_id": attempt_id,
            "generation_type": generation_type,
            "provider_name": provider_name,
            "provider_profile": provider_profile,
            "phase": phase,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "remote_request_id_digest": remote_digest,
            "recovery": recovery,
            "maintenance_task": maintenance_task,
            "status": status,
            "error_type": error_type,
        },
    )
