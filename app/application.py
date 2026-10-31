from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.router import router
from app.config import Settings, get_settings
from app.conversation.service import ConversationService
from app.db import Database
from app.errors import install_error_handlers
from app.health import DatabaseReadinessProbe, ReadinessProbe
from app.logging import configure_logging
from app.middleware import install_request_middleware
from app.ops.service import OpsService
from app.providers.payswift import PaySwiftClient

logger = logging.getLogger(__name__)

WORKER_NAME = "payout-worker"


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    readiness_probe: ReadinessProbe | None = None,
    payswift_client: PaySwiftClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_api_runtime()
    configure_logging(settings.log_level)
    database = database or Database(settings.database_url)
    readiness_probe = readiness_probe or DatabaseReadinessProbe(
        database,
        worker_name=WORKER_NAME,
        heartbeat_ttl_seconds=settings.worker_heartbeat_ttl_seconds,
    )
    payswift_client = payswift_client or PaySwiftClient(settings)
    conversation_service = ConversationService(
        database,
        settings,
        payswift_client=payswift_client,
    )
    ops_service = OpsService(database, settings, payswift_client)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("application_started", extra=settings.safe_summary())
        yield
        database.dispose()
        logger.info("application_stopped")

    app = FastAPI(
        title="QuickDrop Rider Payout Dispute Desk",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.readiness_probe = readiness_probe
    app.state.conversation_service = conversation_service
    app.state.ops_service = ops_service
    install_request_middleware(app)
    install_error_handlers(app)
    app.include_router(router)
    return app
