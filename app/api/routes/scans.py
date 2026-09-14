"""Start and inspect a durable per-project MinIO scan."""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.api.error_mapping import run_application
from app.application.errors import ResourceNotFoundError
from app.application.scan_service import KnowledgeScanner, latest_scan, scan_prefix, start_scan
from app.core.container import application_container
from app.db import SessionLocal, get_db
from app.db.models import Project

router = APIRouter(tags=["documents"])


def snapshot(scan):
    if scan is None:
        return None
    fields = [
        "id",
        "status",
        "processed",
        "imported",
        "skipped",
        "failed",
        "current_key",
        "details",
        "error",
    ]
    return {name: getattr(scan, name) for name in fields}


def run_scan(scan_id):
    container = application_container()
    with SessionLocal() as db:
        KnowledgeScanner(container.settings, container.storage, container.document_application).run(
            db, scan_id
        )


@router.get("/projects/{project_id}/scan")
def scan_status(project_id: str, db: Session = Depends(get_db)):
    def action():
        if db.get(Project, project_id) is None:
            raise ResourceNotFoundError("Project not found")
        settings = application_container().settings
        return {
            "enabled": settings.storage_backend == "s3" and settings.run_inline_ingestion,
            "bucket": settings.s3_bucket,
            "prefix": scan_prefix(project_id),
            "scan": snapshot(latest_scan(db, project_id)),
        }

    return run_application(action)


@router.post("/projects/{project_id}/scan", status_code=202)
def scan_project(project_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    scan, created = run_application(
        lambda: start_scan(db, project_id, application_container().settings)
    )
    if created:
        background.add_task(run_scan, scan.id)
    return snapshot(scan)
