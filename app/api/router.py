"""Public API router composition.

Route implementations live in capability-focused modules under
``app.api.routes``. This module is the only place that assembles the public
HTTP route tree.
"""

from fastapi import APIRouter

from app.api.routes import chat, documents, health, operations

router = APIRouter()
router.include_router(documents.router)
router.include_router(chat.router)
router.include_router(operations.router)

# Health endpoints intentionally remain outside /api/v1 for orchestrators.
health_router = health.router
