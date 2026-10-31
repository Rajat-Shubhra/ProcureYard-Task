from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.messages import router as messages_router
from app.api.ops import router as ops_router
from app.api.trace import router as trace_router

router = APIRouter()
router.include_router(health_router)
router.include_router(messages_router)
router.include_router(trace_router)
router.include_router(ops_router)
