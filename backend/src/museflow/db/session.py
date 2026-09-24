from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 5,
            "keepalives": 1,
            "keepalives_idle": 5,
            "keepalives_interval": 1,
            "keepalives_count": 3,
        },
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
