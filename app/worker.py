from __future__ import annotations

import logging
import signal
from datetime import UTC, datetime
from threading import Event, Thread

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


def heartbeat_loop(database: Database, stop: Event, interval_seconds: float) -> None:
    """Refresh the liveness row every ``interval_seconds`` until ``stop`` is set.

    Runs on its own thread so a slow tick (PaySwift outage path, many intents) cannot starve
    the heartbeat and flip ``/health`` to ``worker_heartbeat_stale``.
    """

    while True:
        try:
            _write_heartbeat(database)
        except Exception as exc:
            logger.exception(
                "worker_heartbeat_failed",
                extra={"worker_name": WORKER_NAME, "error_type": type(exc).__name__},
            )
        if stop.wait(interval_seconds):
            return


def start_heartbeat_thread(database: Database, stop: Event, interval_seconds: float) -> Thread:
    thread = Thread(
        target=heartbeat_loop,
        args=(database, stop, interval_seconds),
        name="payout-worker-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


def run_outbox_loop(outbox: PayoutOutboxWorker, stop: Event, poll_interval_seconds: float) -> None:
    """Drive ``tick`` until ``stop`` is set; a raising tick is logged and retried after a poll."""

    while not stop.is_set():
        try:
            processed = outbox.tick(should_stop=stop.is_set)
        except Exception as exc:
            logger.exception(
                "worker_tick_failed",
                extra={"worker_name": WORKER_NAME, "error_type": type(exc).__name__},
            )
            processed = 0
        if processed == 0:
            stop.wait(poll_interval_seconds)


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

    heartbeat = start_heartbeat_thread(database, stop, settings.worker_heartbeat_interval_seconds)
    try:
        run_outbox_loop(outbox, stop, settings.worker_poll_interval_seconds)
    finally:
        stop.set()
        heartbeat.join(timeout=settings.worker_heartbeat_interval_seconds + 1)
        database.dispose()
        logger.info("worker_stopped", extra={"worker_name": WORKER_NAME})


if __name__ == "__main__":
    run()
