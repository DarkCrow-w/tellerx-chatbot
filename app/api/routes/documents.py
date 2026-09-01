"""文档目录、上传、生命周期和下载 Controller。"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.error_mapping import run_application
from app.application.document_service import UploadDocumentCommand
from app.contracts.schemas import (
    BulkDeleteDocumentsIn,
    BulkDeleteDocumentsOut,
    DocumentCapabilitiesOut,
    DocumentPageOut,
    JobOut,
    ProjectNameIn,
    ProjectOut,
    SourceOut,
    UploadResponse,
    VersionOut,
)
from app.core.container import document_application_service
from app.db import SessionLocal, get_db

router = APIRouter(tags=["documents"])
logger = logging.getLogger(__name__)


def _run_job(job_id: str) -> None:
    """在独立数据库会话中执行仅供开发环境使用的内联任务。"""

    logger.info("后台入库任务开始 job_id=%s", job_id)
    try:
        with SessionLocal() as db:
            document_application_service().process_ingestion_job(db, job_id)
    except Exception:
        logger.exception("后台入库任务异常 job_id=%s", job_id)
        raise
    logger.info("后台入库任务完成 job_id=%s", job_id)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list:
    """返回可供前端选择的知识库项目。"""

    return document_application_service().list_projects(db)


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectNameIn, db: Session = Depends(get_db)) -> ProjectOut:
    """创建一个尚未包含文档的知识库。"""

    return run_application(
        lambda: document_application_service().create_project(db, payload.name)
    )


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def rename_project(
    project_id: str,
    payload: ProjectNameIn,
    db: Session = Depends(get_db),
) -> ProjectOut:
    """修改知识库显示名称，不改变其检索范围 ID。"""

    return run_application(
        lambda: document_application_service().rename_project(db, project_id, payload.name)
    )


@router.get("/projects/{project_id}/documents", response_model=DocumentPageOut)
def list_project_documents(
    project_id: str,
    q: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> DocumentPageOut:
    """返回知识库下用于管理的分页文档摘要。"""

    return run_application(
        lambda: document_application_service().list_documents(
            db,
            project_id=project_id,
            query=q,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/documents/capabilities", response_model=DocumentCapabilitiesOut)
def document_capabilities() -> DocumentCapabilitiesOut:
    """返回前端上传前需要使用的文件类型和大小限制。"""

    return document_application_service().document_capabilities()


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


@router.post(
    "/projects/{project_id}/documents/bulk-delete",
    response_model=BulkDeleteDocumentsOut,
)
def bulk_delete_documents(
    project_id: str,
    payload: BulkDeleteDocumentsIn,
    db: Session = Depends(get_db),
) -> BulkDeleteDocumentsOut:
    """批量软删除当前知识库中勾选的逻辑文档。"""

    return run_application(
        lambda: document_application_service().bulk_delete_documents(
            db,
            project_id=project_id,
            document_ids=payload.document_ids,
        )
    )
