from __future__ import annotations

import os
from typing import Any, cast

from redis import Redis  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import text

from museflow.db.session import create_session_factory


def check_runtime_dependencies() -> None:
    database_url = os.environ.get(
        "MUSEFLOW_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/museflow",
    )
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    factory = create_session_factory(database_url)
    with factory() as session:
        session.execute(text("SELECT 1"))
    client = cast(Any, Redis.from_url(redis_url))  # pyright: ignore[reportUnknownMemberType]
    try:
        client.ping()
    finally:
        client.close()


def runtime_probe() -> int:
    try:
        check_runtime_dependencies()
    except Exception:
        return 1
    return 0
