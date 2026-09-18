from __future__ import annotations

import os
import sys
from uuid import UUID

from celery import Celery  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.session import create_session_factory
from museflow.providers import MockProvider
from museflow.queue import create_celery_app
from museflow.runtime import runtime_probe
from museflow.tasks.execution import ExecuteGenerationAttempt

celery_app: Celery = create_celery_app()


def _session_factory() -> sessionmaker[Session]:
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    return create_session_factory(database_url)


@celery_app.task(name="museflow.execute_task", ignore_result=True)  # pyright: ignore[reportUntypedFunctionDecorator,reportUnknownMemberType]
def execute_task(task_id: str) -> None:
    ExecuteGenerationAttempt(_session_factory(), MockProvider()).execute(UUID(task_id))


if __name__ == "__main__" and "--check-ready" in sys.argv:
    raise SystemExit(runtime_probe())
