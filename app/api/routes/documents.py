"""文档目录、上传、生命周期和下载 Controller。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.error_mapping import run_application
from app.application.document_service import UploadDocumentCommand
from app.contracts.schemas import JobOut, ProjectOut, SourceOut, UploadResponse, VersionOut
from app.core.container import document_application_service
from app.db import SessionLocal, get_db

router = APIRouter(tags=["documents"])


def _run_job(job_id: str) -> None:
    """在独立数据库会话中执行仅供开发环境使用的内联任务。"""

    with SessionLocal() as db:
        document_application_service().process_ingestion_job(db, job_id)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list:
    """返回可供前端选择的知识库项目。"""

    return document_application_service().list_projects(db)


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
) -> UploadResponse:
    """把传输字段转换为上传命令，并返回异步任务标识。"""

    command = UploadDocumentCommand(
        stream=file.file,
        filename=file.filename or "document",
        project=project,
        document_type=document_type,
        lifecycle_status=lifecycle_status,
        version_label=version_label,
        logical_key=logical_key,
        effective_at=effective_at,
        owner=owner,
        supersedes_document_id=supersedes_document_id,
    )
    result = run_application(lambda: document_application_service().upload(db, command))
    if result.inline_job_id:
        background_tasks.add_task(_run_job, result.inline_job_id)
    return result.response


@router.get("/ingestion-jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """查询入库任务的阶段、进度和失败信息。"""

    return run_application(lambda: document_application_service().get_job(db, job_id))


@router.post("/ingestion-jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """为已结束任务创建新的入库尝试。"""

    service = document_application_service()
    retry = run_application(lambda: service.retry_job(db, job_id))
    if service.settings.run_inline_ingestion:
        background_tasks.add_task(_run_job, retry.id)
    return retry


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def list_document_versions(document_id: str, db: Session = Depends(get_db)) -> list:
    """返回指定文档的全部版本。"""

    return run_application(
        lambda: document_application_service().list_versions(db, document_id)
    )


@router.post("/document-versions/{version_id}/approve", response_model=VersionOut)
def approve_document_version(version_id: str, db: Session = Depends(get_db)):
    """请求批准已完成索引校验的版本。"""

    return run_application(
        lambda: document_application_service().approve_version(db, version_id)
    )


@router.post("/document-versions/{version_id}/deprecate", response_model=VersionOut)
def deprecate_document_version(version_id: str, db: Session = Depends(get_db)):
    """废弃版本并发布搜索投影删除事件。"""

    return run_application(
        lambda: document_application_service().deprecate_version(db, version_id)
    )


@router.get("/sources/{chunk_id}", response_model=SourceOut)
def get_source(chunk_id: str, db: Session = Depends(get_db)) -> SourceOut:
    """返回引用分块的原文及定位信息。"""

    return run_application(lambda: document_application_service().get_source(db, chunk_id))


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: str,
    version_id: str | None = None,
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载指定版本；未指定版本时返回最近上传版本。"""

    result = run_application(
        lambda: document_application_service().download(
            db,
            document_id=document_id,
            version_id=version_id,
        )
    )
    return FileResponse(result.path, filename=result.filename)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)) -> None:
    """软删除文档，并异步移除全部版本的搜索投影。"""

    run_application(lambda: document_application_service().delete_document(db, document_id))
