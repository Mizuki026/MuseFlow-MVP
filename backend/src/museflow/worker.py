from __future__ import annotations

import logging
import os
import sys
from typing import Any, cast
from uuid import UUID

from celery import Celery  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.orm import Session, sessionmaker

from museflow.assets import MinioResultAssetStore
from museflow.db.models import GenerationTaskModel
from museflow.db.session import create_session_factory
from museflow.providers import create_provider_for_task
from museflow.queue import create_celery_app
from museflow.reference_assets.blob_store import MinioBlobStore
from museflow.reference_assets.maintenance import ReferenceAssetMaintenance
from museflow.runtime import worker_runtime_probe
from museflow.tasks.execution import ExecuteGenerationAttempt
from museflow.tasks.result_publication import ResultPublicationStatus

logger = logging.getLogger(__name__)

celery_app: Celery = create_celery_app()


def _session_factory() -> sessionmaker[Session]:
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    return create_session_factory(database_url)


@celery_app.task(name="museflow.execute_task", ignore_result=True)  # pyright: ignore[reportUntypedFunctionDecorator,reportUnknownMemberType]
def execute_task(task_id: str) -> None:
    factory = _session_factory()
    with factory() as session:
        task = session.get(GenerationTaskModel, UUID(task_id))
        profile_values = (
            (
                task.provider_profile,
                task.provider_name,
                task.model_name,
                task.capability_version,
                task.execution_profile,
            )
            if task is not None
            else ("", "", "", "", None)
        )
    provider = create_provider_for_task(
        profile_id=profile_values[0],
        provider_name=profile_values[1],
        model_name=profile_values[2],
        capability_version=profile_values[3],
        execution_profile=profile_values[4],
    )
    try:
        outcome = ExecuteGenerationAttempt(
            factory,
            provider,
            asset_store=MinioResultAssetStore(),
        ).execute(UUID(task_id))
        if outcome.publication_status is ResultPublicationStatus.OWNERSHIP_LOST:
            logger.info(
                "result candidate was not published because the worker lost ownership",
                extra={
                    "task_id": str(outcome.task_id),
                    "attempt_id": str(outcome.attempt_id),
                    "publication_status": outcome.publication_status.value,
                    "candidate_persisted": outcome.candidate_persisted,
                },
            )
        elif outcome.publication_status is ResultPublicationStatus.CONFLICTING_RESULT:
            logger.error(
                "result candidate conflicts with the existing authoritative result",
                extra={
                    "task_id": str(outcome.task_id),
                    "attempt_id": str(outcome.attempt_id),
                    "publication_status": outcome.publication_status.value,
                    "candidate_persisted": outcome.candidate_persisted,
                },
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


@cast(Any, celery_app).task(
    name="museflow.reference_asset_maintenance",
    ignore_result=True,
    time_limit=120,
    soft_time_limit=90,
)
def reference_asset_maintenance(message_type: str, asset_id: str) -> None:
    factory = _session_factory()
    maintenance = ReferenceAssetMaintenance(factory, MinioBlobStore())
    if message_type == "DELETE_REFERENCE_ASSET":
        deleted = maintenance.delete_asset(UUID(asset_id))
        logger.info(
            "reference asset deletion maintenance finished",
            extra={"asset_id": asset_id, "deleted": deleted},
        )
        return
    if message_type == "RUN_REFERENCE_MAINTENANCE":
        summary = maintenance.run_batch()
        logger.info(
            "reference asset maintenance batch finished",
            extra={
                "staging_ready": summary.staging_ready,
                "staging_failed": summary.staging_failed,
                "storage_retries": summary.storage_retries,
                "deleted": summary.deleted,
                "delete_retries": summary.delete_retries,
            },
        )
        return
    raise ValueError("unsupported reference asset maintenance operation")


if __name__ == "__main__" and "--check-ready" in sys.argv:
    raise SystemExit(worker_runtime_probe())
