from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from museflow.db.models import OutboxMessageModel


class MaintenancePublisher(Protocol):
    def publish(self, message_type: str, asset_id: UUID) -> None: ...


class ReferenceMaintenanceScheduler:
    """Create periodic maintenance intents using PostgreSQL only."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, *, interval_seconds: int = 300) -> bool:
        now = self._clock()
        with self._session_factory.begin() as session:
            last_scheduled = session.scalar(
                select(func.max(OutboxMessageModel.created_at)).where(
                    OutboxMessageModel.message_type == "RUN_REFERENCE_MAINTENANCE"
                )
            )
            if last_scheduled is not None and last_scheduled > now - timedelta(
                seconds=interval_seconds
            ):
                return False
            session.add(
                OutboxMessageModel(
                    id=uuid4(),
                    message_type="RUN_REFERENCE_MAINTENANCE",
                    aggregate_id=uuid4(),
                    payload={"operation": "reference_asset_maintenance"},
                    available_at=now,
                    created_at=now,
                )
            )
        return True


class ReferenceMaintenanceOutboxDispatcher:
    MESSAGE_TYPES = ("RUN_REFERENCE_MAINTENANCE", "DELETE_REFERENCE_ASSET")

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: MaintenancePublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_once(self, *, limit: int = 50) -> int:
        with self._session_factory() as session:
            messages = list(
                session.scalars(
                    select(OutboxMessageModel)
                    .where(
                        OutboxMessageModel.message_type.in_(self.MESSAGE_TYPES),
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
                self._publisher.publish(message.message_type, message.aggregate_id)
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
