"""Document catalog, lifecycle, upload, source, and download endpoints.

PostgreSQL and original object storage remain authoritative.  Every lifecycle
change that affects search visibility is published through the transactional
outbox instead of writing the PostgreSQL search projection from the request handler.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.contracts.schemas import JobOut, ProjectOut, SourceOut, UploadResponse, VersionOut
from app.core.config import Settings, get_settings
from app.core.container import ingestion_service
from app.db import SessionLocal, get_db
from app.db.models import Chunk, Document, DocumentVersion, IngestionJob, OutboxEvent, Project
from app.integrations.storage import LocalObjectStorage
from app.knowledge.parsers import DocumentParser

router = APIRouter(tags=["documents"])


def _run_job(job_id: str) -> None:
    """使用独立数据库会话执行一个仅供开发环境使用的内联任务。"""

    with SessionLocal() as db:
        ingestion_service().process(db, job_id)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    """按名称返回可供前端选择的知识库项目。"""

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
    """持久化不可变文件版本，并创建异步入库任务。"""

    if lifecycle_status not in {"draft", "approved", "deprecated"}:
        raise HTTPException(422, "lifecycle_status must be draft, approved, or deprecated")
    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower()
    if suffix not in DocumentParser.allowed_suffixes:
        raise HTTPException(415, f"Unsupported file type: {suffix}")

    storage = LocalObjectStorage(settings.storage_root)
    try:
        path, sha256, _ = storage.save(file.file, filename, settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc

    project_name = project.strip()
    project_row = db.scalar(select(Project).where(Project.name == project_name))
    if not project_row:
        try:
            with db.begin_nested():
                project_row = Project(name=project_name)
                db.add(project_row)
                db.flush()
        except IntegrityError:
            # 并发的首次上传可能都判断项目不存在；savepoint 仅回滚失败插入，随后
            # 复用胜出事务提交的项目，避免把正常竞争返回为 HTTP 500。
            project_row = db.scalar(select(Project).where(Project.name == project_name))
            if not project_row:
                raise
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
        # 软删除不抹去逻辑身份；重新上传时恢复原文档，避免产生相同 logical_key 的歧义副本。
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

    version = DocumentVersion(
        document_id=document.id,
        sha256=sha256,
        storage_path=str(path.relative_to(settings.storage_root)),
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
    """查询入库任务的阶段、进度和失败信息。"""

    job = db.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(404, "Ingestion job not found")
    return job


@router.post("/ingestion-jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> IngestionJob:
    """创建新的不可变尝试记录，不覆盖原任务历史。"""

    job = db.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(404, "Ingestion job not found")
    if job.status not in {"failed", "succeeded"}:
        raise HTTPException(409, "Only completed jobs can be re-queued")
    retry = IngestionJob(document_id=job.document_id, version_id=job.version_id)
    db.add(retry)
    db.commit()
    return retry


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def list_document_versions(
    document_id: str, db: Session = Depends(get_db)
) -> list[DocumentVersion]:
    """按创建时间倒序返回未删除文档的全部版本。"""

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
    """请求批准已验证版本；实际 current 切换由索引 Worker 原子完成。"""

    version = db.get(DocumentVersion, version_id)
    if not version:
        raise HTTPException(404, "Document version not found")
    if version.technical_status != "searchable":
        raise HTTPException(409, "Only a fully indexed and verified version can be approved")
    # 只有替换版本发布完成且分块数量校验通过后，索引 Worker 才会切换 is_current。
    version.lifecycle_status = "approved"
    version.is_current = False
    db.add(OutboxEvent(aggregate_id=version.id, event_type="index_version", payload={}))
    db.commit()
    return version


@router.post("/document-versions/{version_id}/deprecate", response_model=VersionOut)
def deprecate_document_version(version_id: str, db: Session = Depends(get_db)) -> DocumentVersion:
    """废弃文档版本，并通过 Outbox 异步移除其搜索投影。"""

    version = db.get(DocumentVersion, version_id)
    if not version:
        raise HTTPException(404, "Document version not found")
    version.lifecycle_status = "deprecated"
    version.is_current = False
    version.effective_to = datetime.now(version.created_at.tzinfo)
    db.add(OutboxEvent(aggregate_id=version.id, event_type="delete_version", payload={}))
    db.commit()
    return version


@router.get("/sources/{chunk_id}", response_model=SourceOut)
def get_source(chunk_id: str, db: Session = Depends(get_db)) -> SourceOut:
    """返回引用分块的原文与可定位来源信息。"""

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
    """下载指定版本；未指定版本时返回最近上传版本。"""

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
    """软删除目录记录，并异步移除所有版本的搜索投影。"""

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
