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
from museflow.reference_assets.access import ReferenceAssetReader
from museflow.reference_assets.blob_store import MinioBlobStore
from museflow.reference_assets.maintenance import ReferenceAssetMaintenance
from museflow.runtime import worker_runtime_probe
from museflow.safe_logging import log_task_event
from museflow.tasks.domain import GenerationType
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
                task.generation_type,
            )
            if task is not None
            else ("", "", "", "", None, GenerationType.TEXT_TO_IMAGE.value)
        )
    provider = create_provider_for_task(
        profile_id=profile_values[0],
        provider_name=profile_values[1],
        model_name=profile_values[2],
        capability_version=profile_values[3],
        execution_profile=profile_values[4],
        generation_type=GenerationType(profile_values[5]),
    )
    log_task_event(
        logger,
        "provider_adapter_ready",
        task_id=task_id,
        generation_type=profile_values[5],
        provider_name=profile_values[1],
        provider_profile=profile_values[0],
        status="ready",
    )
    try:
        reference_blobs = MinioBlobStore()
        outcome = ExecuteGenerationAttempt(
            factory,
            provider,
            asset_store=MinioResultAssetStore(),
            reference_reader=ReferenceAssetReader(factory, reference_blobs),
        ).execute(UUID(task_id))
        if outcome.publication_status is ResultPublicationStatus.OWNERSHIP_LOST:
            log_task_event(
                logger,
                "result_candidate_not_published",
                level=logging.WARNING,
                task_id=str(outcome.task_id),
                attempt_id=str(outcome.attempt_id) if outcome.attempt_id else None,
                generation_type=profile_values[5],
                provider_name=profile_values[1],
                provider_profile=profile_values[0],
                error_code=outcome.publication_status.value,
                recovery=outcome.candidate_persisted,
                status=outcome.publication_status.value.lower(),
            )
        elif outcome.publication_status is ResultPublicationStatus.CONFLICTING_RESULT:
            log_task_event(
                logger,
                "result_candidate_conflict",
                level=logging.ERROR,
                task_id=str(outcome.task_id),
                attempt_id=str(outcome.attempt_id) if outcome.attempt_id else None,
                generation_type=profile_values[5],
                provider_name=profile_values[1],
                provider_profile=profile_values[0],
                error_code=outcome.publication_status.value,
                recovery=outcome.candidate_persisted,
                status=outcome.publication_status.value.lower(),
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
        log_task_event(
            logger,
            "reference_asset_deleted" if deleted else "reference_asset_delete_noop",
            asset_id=asset_id,
            maintenance_task=True,
            status="deleted" if deleted else "unchanged",
        )
        return
    if message_type == "RUN_REFERENCE_MAINTENANCE":
        summary = maintenance.run_batch()
        log_task_event(
            logger,
            "reference_asset_maintenance_batch_finished",
            maintenance_task=True,
            error_code=(
                "MAINTENANCE_BATCH_PARTIAL"
                if summary.staging_failed or summary.storage_retries or summary.delete_retries
                else None
            ),
            status=(
                "completed_with_errors"
                if summary.staging_failed or summary.storage_retries or summary.delete_retries
                else "completed"
            ),
        )
        return
    raise ValueError("unsupported reference asset maintenance operation")


if __name__ == "__main__" and "--check-ready" in sys.argv:
    raise SystemExit(worker_runtime_probe())
