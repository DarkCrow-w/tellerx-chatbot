"""存活与依赖就绪 Controller。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.container import health_application_service
from app.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    """仅确认 API 进程仍能处理请求。"""

    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    """仅当数据库和搜索结构都可用时通过就绪检查。"""

    payload = health_application_service().readiness(db)
    if payload["status"] != "ready":
        raise HTTPException(503, payload)
    return payload
