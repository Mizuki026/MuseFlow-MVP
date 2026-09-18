from __future__ import annotations

import os
import sys
import time

from museflow.db.session import create_session_factory
from museflow.queue import CeleryTaskPublisher, create_celery_app
from museflow.runtime import runtime_probe
from museflow.tasks.dispatcher import OutboxDispatcher


def run_scheduler() -> None:
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    interval = float(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "1"))
    celery_app = create_celery_app()
    dispatcher = OutboxDispatcher(
        create_session_factory(database_url), CeleryTaskPublisher(celery_app)
    )
    while True:
        dispatcher.dispatch_once()
        time.sleep(interval)


if __name__ == "__main__":
    if "--check-ready" in sys.argv:
        raise SystemExit(runtime_probe())
    run_scheduler()
