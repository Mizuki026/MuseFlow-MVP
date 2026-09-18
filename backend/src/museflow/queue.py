from __future__ import annotations

import os
from typing import Any, Protocol, cast
from uuid import UUID

from celery import Celery  # pyright: ignore[reportMissingTypeStubs]


class TaskPublisher(Protocol):
    def publish(self, task_id: UUID) -> None: ...


class CeleryTaskPublisher:
    def __init__(self, celery_app: Celery, *, task_name: str = "museflow.execute_task") -> None:
        self._celery_app = celery_app
        self._task_name = task_name

    def publish(self, task_id: UUID) -> None:
        cast(Any, self._celery_app).send_task(self._task_name, args=[str(task_id)])


def create_celery_app(redis_url: str | None = None) -> Celery:
    broker_url = redis_url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    return Celery(
        "museflow",
        broker=broker_url,
        backend=None,
        include=["museflow.worker"],
    )
