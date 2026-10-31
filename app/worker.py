from __future__ import annotations

import logging
import signal
from datetime import UTC, datetime
from threading import Event

from sqlalchemy.dialects.postgresql import insert

from app.application import WORKER_NAME
from app.config import get_settings
from app.db import Database
from app.logging import configure_logging
from app.models import WorkerHeartbeat
from app.providers.payswift import PayoutOutboxWorker, PaySwiftClient

logger = logging.getLogger(__name__)


def _write_heartbeat(database: Database) -> None:
    statement = insert(WorkerHeartbeat).values(
        worker_name=WORKER_NAME,
        updated_at=datetime.now(UTC),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_name],
        set_={"updated_at": statement.excluded.updated_at},
    )
    with database.session() as session:
        session.execute(statement)
        session.commit()


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    outbox = PayoutOutboxWorker(database, settings, PaySwiftClient(settings))
    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("worker_started", extra={"worker_name": WORKER_NAME})

    try:
        while not stop.is_set():
            try:
                _write_heartbeat(database)
                outbox.tick()
                _write_heartbeat(database)
            except Exception as exc:
                logger.exception(
                    "worker_heartbeat_failed",
                    extra={"worker_name": WORKER_NAME, "error_type": type(exc).__name__},
                )
            stop.wait(settings.worker_heartbeat_interval_seconds)
    finally:
        database.dispose()
        logger.info("worker_stopped", extra={"worker_name": WORKER_NAME})


if __name__ == "__main__":
    run()
