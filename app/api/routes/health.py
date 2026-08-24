"""Liveness and dependency-aware readiness probes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.container import search_index
from app.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    """Confirm only that the API process can serve requests."""

    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    """Fail readiness unless PostgreSQL and its FTS/pgvector schema are available."""

    try:
        db.execute(text("SELECT 1"))
        database = True
    except SQLAlchemyError:
        database = False

    search_state = search_index().status()
    search = bool(search_state.get("available") and search_state.get("table_ready"))
    status = "ready" if database and search else "not_ready"
    payload = {"status": status, "database": database, "search": search}
    if status != "ready":
        raise HTTPException(503, payload)
    return payload
