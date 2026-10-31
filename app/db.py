from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """Owns the SQLAlchemy engine and short-lived session factory."""

    def __init__(self, database_url: str) -> None:
        connect_args: dict[str, object] = {}
        if database_url.startswith("postgresql"):
            # A black-holed database must surface as a retryable 503 quickly, not hang the
            # webhook past the vendor's ~10 s retry window.
            connect_args["connect_timeout"] = 2
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args=connect_args,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        with self.session_factory() as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()


def lock_rider(session: Session, rider_id: str) -> None:
    """Serialize all state changes for one rider inside the current transaction.

    PostgreSQL uses a transaction-scoped advisory lock shared by the message path and the ops
    review path. SQLite is single-writer, so no explicit lock is needed there.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:rider_id))"), {"rider_id": rider_id}
        )
