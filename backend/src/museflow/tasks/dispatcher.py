from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import OutboxMessageModel
from museflow.queue import TaskPublisher
from museflow.safe_logging import log_task_event

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: TaskPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_once(self, *, limit: int = 100) -> int:
        with self._session_factory() as session:
            messages = list(
                session.scalars(
                    select(OutboxMessageModel)
                    .where(
                        OutboxMessageModel.published_at.is_(None),
                        OutboxMessageModel.available_at <= self._clock(),
                        OutboxMessageModel.message_type == "EXECUTE_TASK",
                    )
                    .order_by(OutboxMessageModel.created_at.asc(), OutboxMessageModel.id.asc())
                    .limit(limit)
                )
            )

        published = 0
        for message in messages:
            try:
                task_id = UUID(cast(str, message.payload["task_id"]))
                self._publisher.publish(task_id)
            except Exception as error:
                log_task_event(
                    logger,
                    "task_outbox_delivery_failed",
                    level=logging.ERROR,
                    task_id=str(message.aggregate_id),
                    error_code="OUTBOX_DELIVERY_FAILED",
                    error_type=type(error).__name__,
                    status="pending",
                )
                continue
            with self._session_factory.begin() as session:
                result = session.execute(
                    update(OutboxMessageModel)
                    .where(
                        OutboxMessageModel.id == message.id,
                        OutboxMessageModel.published_at.is_(None),
                    )
                    .values(published_at=self._clock())
                )
            if cast(Any, getattr(result, "rowcount", 0)) == 1:
                published += 1
                log_task_event(
                    logger,
                    "task_outbox_published",
                    task_id=str(message.aggregate_id),
                    status="published",
                )
        return published
