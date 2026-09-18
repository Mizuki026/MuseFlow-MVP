from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import OutboxMessageModel
from museflow.queue import TaskPublisher


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
            except Exception:
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
        return published
