from __future__ import annotations

import logging
import os
import sys
from uuid import UUID

from celery import Celery  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.orm import Session, sessionmaker

from museflow.assets import MinioResultAssetStore
from museflow.db.models import GenerationTaskModel
from museflow.db.session import create_session_factory
from museflow.providers import MockProvider, create_provider_from_environment
from museflow.queue import create_celery_app
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
        scenario = task.execution_profile if task is not None else None
    provider = MockProvider(scenario=scenario) if scenario else create_provider_from_environment()
    try:
        outcome = ExecuteGenerationAttempt(
            factory, provider, asset_store=MinioResultAssetStore()
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


if __name__ == "__main__" and "--check-ready" in sys.argv:
    raise SystemExit(worker_runtime_probe())
