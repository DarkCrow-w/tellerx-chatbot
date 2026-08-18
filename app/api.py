from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.answering import AnswerValidationError
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.dependencies import (
    answer_service,
    indexing_service,
    ingestion_service,
    model_router,
    search_index,
)
from app.model_router import NoModelAvailable
from app.models import (
    AnswerFeedback,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    IndexSyncState,
    IngestionJob,
    Message,
    OutboxEvent,
    Project,
)
from app.parsers import DocumentParser
from app.schemas import (
    ChatRequest,
    ChatResponse,
    FeedbackIn,
    IndexStatusOut,
    JobOut,
    ProjectOut,
    SourceOut,
    UploadResponse,
    UsageOut,
    VersionOut,
)
from app.storage import LocalObjectStorage

router = APIRouter()


def _run_job(job_id: str) -> None:
    with SessionLocal() as db:
        ingestion_service().process(db, job_id)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.name)))


@router.post("/documents", response_model=UploadResponse, status_code=202)
def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    project: str = Form(..., min_length=1, max_length=200),
    document_type: str = Form(..., min_length=1, max_length=100),
    lifecycle_status: str = Form(...),
    version_label: str | None = Form(default=None, max_length=100),
    logical_key: str | None = Form(default=None, max_length=500),
    effective_at: datetime | None = Form(default=None),
    owner: str | None = Form(default=None, max_length=200),
    supersedes_document_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    if lifecycle_status not in {"draft", "approved", "deprecated"}:
        raise HTTPException(422, "lifecycle_status must be draft, approved, or deprecated")
    filename = file.filename or "document"
    if Path(filename).suffix.lower() not in DocumentParser.allowed_suffixes:
        raise HTTPException(415, f"Unsupported file type: {Path(filename).suffix.lower()}")
    storage = LocalObjectStorage(settings.storage_root)
    try:
        path, sha256, _ = storage.save(file.file, filename, settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc

    project_row = db.scalar(select(Project).where(Project.name == project.strip()))
    if not project_row:
        project_row = Project(name=project.strip())
        db.add(project_row)
        db.flush()
    resolved_logical_key = (logical_key or Path(filename).name).strip()
    document = db.scalar(
        select(Document).where(
            Document.project_id == project_row.id,
            Document.logical_key == resolved_logical_key,
        )
    )
    if not document:
        document = Document(
            project_id=project_row.id,
            logical_key=resolved_logical_key,
            filename=Path(filename).name,
            document_type=document_type.strip(),
            owner=owner,
        )
        db.add(document)
        db.flush()
    elif document.is_deleted:
        # A logical key is stable for its lifetime. Re-uploading after a soft
        # delete restores that identity instead of violating the unique key or
        # silently creating a second logical document.
        document.is_deleted = False
        document.filename = Path(filename).name
        document.document_type = document_type.strip()
        document.owner = owner
    duplicate = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.sha256 == sha256,
        )
    )
    if duplicate:
        existing_job = db.scalar(
            select(IngestionJob)
            .where(IngestionJob.version_id == duplicate.id)
            .order_by(IngestionJob.created_at.desc())
        )
        if (
            not existing_job
            or existing_job.status == "failed"
            or duplicate.technical_status in {"deleted", "failed_final"}
        ):
            existing_job = IngestionJob(document_id=document.id, version_id=duplicate.id)
            db.add(existing_job)
            db.commit()
        return UploadResponse(
            document_id=document.id,
            version_id=duplicate.id,
            job_id=existing_job.id,
            duplicate=True,
        )
    relative_path = str(path.relative_to(settings.storage_root))
    version = DocumentVersion(
        document_id=document.id,
        sha256=sha256,
        storage_path=relative_path,
        lifecycle_status=lifecycle_status,
        technical_status="received",
        version_label=version_label,
        effective_at=effective_at,
        supersedes_document_id=supersedes_document_id,
    )
    db.add(version)
    db.flush()
    job = IngestionJob(document_id=document.id, version_id=version.id)
    db.add(job)
    db.commit()
    if settings.run_inline_ingestion:
        job.status = "running"
        db.commit()
        background_tasks.add_task(_run_job, job.id)
    return UploadResponse(document_id=document.id, version_id=version.id, job_id=job.id)


@router.get("/ingestion-jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> IngestionJob:
    job = db.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(404, "Ingestion job not found")
    return job


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def list_document_versions(document_id: str, db: Session = Depends(get_db)) -> list[DocumentVersion]:
    document = db.get(Document, document_id)
    if not document or document.is_deleted:
        raise HTTPException(404, "Document not found")
    return list(
        db.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.created_at.desc())
        )
    )


@router.post("/document-versions/{version_id}/approve", response_model=VersionOut)
def approve_document_version(version_id: str, db: Session = Depends(get_db)) -> DocumentVersion:
    version = db.get(DocumentVersion, version_id)
    if not version:
        raise HTTPException(404, "Document version not found")
    if version.technical_status != "searchable":
        raise HTTPException(409, "Only a fully indexed and verified version can be approved")
    # Keep the old approved version searchable until the replacement has been
    # written to Elasticsearch and its chunk count has been verified. The
    # indexer performs the current-version switch after successful publication.
    version.lifecycle_status = "approved"
    version.is_current = False
    db.add(OutboxEvent(aggregate_id=version.id, event_type="index_version", payload={}))
    db.commit()
    return version


@router.post("/document-versions/{version_id}/deprecate", response_model=VersionOut)
def deprecate_document_version(version_id: str, db: Session = Depends(get_db)) -> DocumentVersion:
    version = db.get(DocumentVersion, version_id)
    if not version:
        raise HTTPException(404, "Document version not found")
    version.lifecycle_status = "deprecated"
    version.is_current = False
    version.effective_to = datetime.now(version.created_at.tzinfo)
    db.add(OutboxEvent(aggregate_id=version.id, event_type="delete_version", payload={}))
    db.commit()
    return version


@router.post("/ingestion-jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> IngestionJob:
    job = db.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(404, "Ingestion job not found")
    if job.status not in {"failed", "succeeded"}:
        raise HTTPException(409, "Only completed jobs can be re-queued")
    retry = IngestionJob(document_id=job.document_id, version_id=job.version_id)
    db.add(retry)
    db.commit()
    return retry


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    try:
        return answer_service().answer(
            db,
            question=request.question.strip(),
            project_ids=request.project_ids,
            conversation_id=request.conversation_id,
            pinned_model=request.pinned_model,
        )
    except NoModelAvailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except AnswerValidationError as exc:
        raise HTTPException(502, f"Answer validation failed: {exc}") from exc


@router.get("/sources/{chunk_id}", response_model=SourceOut)
def get_source(chunk_id: str, db: Session = Depends(get_db)) -> SourceOut:
    chunk = db.scalar(
        select(Chunk)
        .options(joinedload(Chunk.version).joinedload(DocumentVersion.document))
        .where(Chunk.id == chunk_id)
    )
    if not chunk or chunk.version.document.is_deleted:
        raise HTTPException(404, "Source not found")
    version = chunk.version
    return SourceOut(
        chunk_id=chunk.id,
        document_id=version.document_id,
        version_id=version.id,
        filename=version.document.filename,
        content=chunk.content,
        heading_path=chunk.heading_path,
        page_number=chunk.page_number,
        sheet_name=chunk.sheet_name,
        cell_range=chunk.cell_range,
        lifecycle_status=version.lifecycle_status,
        version_label=version.version_label,
        effective_at=version.effective_at,
    )


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: str,
    version_id: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    statement = (
        select(DocumentVersion)
        .options(joinedload(DocumentVersion.document))
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.created_at.desc())
    )
    if version_id:
        statement = statement.where(DocumentVersion.id == version_id)
    version = db.scalar(statement.limit(1))
    if not version or version.document.is_deleted:
        raise HTTPException(404, "Document not found")
    path = LocalObjectStorage(settings.storage_root).resolve(
        str(settings.storage_root / version.storage_path)
    )
    if not path.exists():
        raise HTTPException(404, "Stored file is missing")
    return FileResponse(path, filename=version.document.filename)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)) -> None:
    document = db.scalar(
        select(Document)
        .options(joinedload(Document.versions))
        .where(Document.id == document_id, Document.is_deleted.is_(False))
    )
    if not document:
        raise HTTPException(404, "Document not found")
    document.is_deleted = True
    for version in document.versions:
        version.is_current = False
        db.add(OutboxEvent(aggregate_id=version.id, event_type="delete_version", payload={}))
    db.commit()


@router.get("/index/status", response_model=IndexStatusOut)
def index_status(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    state = search_index().status()
    eligible = (
        (DocumentVersion.lifecycle_status == "draft")
        | (
            (DocumentVersion.lifecycle_status == "approved")
            & (DocumentVersion.is_current.is_(True))
        )
    )
    has_current_embedding = (
        select(ChunkEmbedding.id)
        .where(
            ChunkEmbedding.chunk_id == Chunk.id,
            ChunkEmbedding.embedding_fingerprint == settings.embedding_fingerprint,
        )
        .exists()
    )
    state["embedding_fingerprint"] = settings.embedding_fingerprint
    state["missing_embeddings"] = int(
        db.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .where(
                DocumentVersion.technical_status == "searchable",
                eligible,
                ~has_current_embedding,
            )
        )
        or 0
    )
    state["pending_events"] = int(
        db.scalar(
            select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.status.in_(["pending", "processing"])
            )
        )
        or 0
    )
    state["dead_events"] = int(
        db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "dead")
        )
        or 0
    )
    state["sync_differences"] = int(
        db.scalar(
            select(func.count()).select_from(IndexSyncState).where(
                (IndexSyncState.status.in_(["pending", "mismatch", "failed"]))
                | (
                    (IndexSyncState.status == "verified")
                    & (IndexSyncState.expected_chunks != IndexSyncState.indexed_chunks)
                )
            )
        )
        or 0
    )
    return state


@router.post("/admin/indexes/reconcile")
def reconcile_index(repair: bool = False, db: Session = Depends(get_db)) -> dict:
    return indexing_service().reconcile(db, repair=repair)


@router.post("/feedback", status_code=201)
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)) -> dict:
    message = db.get(Message, payload.message_id)
    if not message or message.role != "assistant":
        raise HTTPException(404, "Assistant message not found")
    feedback = AnswerFeedback(
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(feedback)
    db.commit()
    return {"id": feedback.id, "status": "recorded"}


@router.get("/models/usage", response_model=list[UsageOut])
def model_usage(db: Session = Depends(get_db)) -> list[dict]:
    return model_router().usage_rows(db)


@router.post("/internal/diagnostics/qwen")
def diagnostics_notice() -> dict:
    return {
        "status": "disabled-over-http",
        "message": "Run `qwen-diagnostics` locally so credentials and paid diagnostics are not exposed via HTTP.",
    }


health_router = APIRouter()


@health_router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@health_router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
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
    if status != "ready":
        raise HTTPException(
            503,
            {"status": status, "database": database, "elasticsearch": elasticsearch},
        )
    return {"status": status, "database": database, "elasticsearch": elasticsearch}
