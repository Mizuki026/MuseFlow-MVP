from __future__ import annotations

import logging
import os
import sys
import time

from museflow.db.session import create_session_factory
from museflow.queue import CeleryMaintenancePublisher, CeleryTaskPublisher, create_celery_app
from museflow.reference_assets.dispatch import (
    ReferenceMaintenanceOutboxDispatcher,
    ReferenceMaintenanceScheduler,
)
from museflow.runtime import runtime_probe
from museflow.tasks.dispatcher import OutboxDispatcher
from museflow.tasks.recovery import RecoverExpiredLeases, ScheduleDueRetries

logger = logging.getLogger(__name__)


def run_scheduler_iteration(
    retries: ScheduleDueRetries,
    leases: RecoverExpiredLeases,
    dispatcher: OutboxDispatcher,
    maintenance_scheduler: ReferenceMaintenanceScheduler,
    maintenance_dispatcher: ReferenceMaintenanceOutboxDispatcher,
) -> None:
    retries.run_once()
    leases.run_once()
    dispatcher.dispatch_once()
    try:
        maintenance_scheduler.run_once()
        maintenance_dispatcher.dispatch_once()
    except Exception:
        logger.exception("reference asset maintenance dispatch failed")


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
    factory = create_session_factory(database_url)
    retries = ScheduleDueRetries(factory)
    leases = RecoverExpiredLeases(factory)
    maintenance_scheduler = ReferenceMaintenanceScheduler(factory)
    maintenance_dispatcher = ReferenceMaintenanceOutboxDispatcher(
        factory, CeleryMaintenancePublisher(celery_app)
    )
    while True:
        run_scheduler_iteration(
            retries, leases, dispatcher, maintenance_scheduler, maintenance_dispatcher
        )
        time.sleep(interval)


if __name__ == "__main__":
    if "--check-ready" in sys.argv:
        raise SystemExit(runtime_probe())
    run_scheduler()
