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
    """Fail readiness if PostgreSQL or required Elasticsearch aliases are unavailable."""

    try:
        db.execute(text("SELECT 1"))
        database = True
    except SQLAlchemyError:
        database = False

    search_state = search_index().status()
    elasticsearch = bool(
        search_state.get("available")
        and search_state.get("cluster_status") in {"green", "yellow"}
        and search_state.get("read_alias")
        and search_state.get("write_alias")
    )
    status = "ready" if database and elasticsearch else "not_ready"
    payload = {"status": status, "database": database, "elasticsearch": elasticsearch}
    if status != "ready":
        raise HTTPException(503, payload)
    return payload
