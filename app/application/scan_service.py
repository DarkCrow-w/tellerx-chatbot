"""Scan external import objects through the existing immutable upload/ingestion pipeline."""

import logging
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.application.document_service import UploadDocumentCommand
from app.application.errors import InvalidRequestError, ResourceNotFoundError
from app.db.models import Document, DocumentVersion, KnowledgeScan, Project, utcnow
from app.knowledge.parsers import DocumentParser

logger = logging.getLogger(__name__)


def scan_prefix(project_id):
    return f"imports/{project_id}/"


def latest_scan(db, project_id):
    scan = db.scalar(
        select(KnowledgeScan)
        .where(KnowledgeScan.project_id == project_id)
        .order_by(KnowledgeScan.created_at.desc())
        .limit(1)
    )
    if scan and scan.status == "running":
        # A crashed worker must not leave the UI permanently disabled.
        age = utcnow().replace(tzinfo=None) - scan.updated_at.replace(tzinfo=None)
        if age > timedelta(hours=1):
            scan.status = "failed"
            scan.error = "扫描任务已中断，请重新扫描。"
            db.commit()
    return scan


def start_scan(db, project_id, settings):
    if settings.storage_backend != "s3":
        raise InvalidRequestError("扫描知识库需要启用 MinIO/S3 存储")
    if not settings.run_inline_ingestion:
        raise InvalidRequestError("当前扫描功能需要启用本地后台入库模式")
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise ResourceNotFoundError("Project not found")
    previous = latest_scan(db, project_id)
    if previous and previous.status == "running":
        db.commit()
        return previous, False
    scan = KnowledgeScan(project_id=project_id)
    db.add(scan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        running = db.scalar(
            select(KnowledgeScan).where(
                KnowledgeScan.project_id == project_id, KnowledgeScan.status == "running"
            )
        )
        if running is None:
            raise
        return running, False
    return scan, True


def ignored_reason(key, size, max_bytes):
    parts = PurePosixPath(key).parts
    if not key or key.endswith("/"):
        return "目录标记"
    if any(part.startswith(".") for part in parts) or parts[-1].lower() in {
        "thumbs.db",
        "desktop.ini",
    }:
        return "隐藏或系统文件"
    if PurePosixPath(key).suffix.lower() not in DocumentParser.allowed_suffixes:
        return "不支持的文件格式"
    if size > max_bytes:
        return "文件超过上传大小限制"
    if len(key) > 490:
        return "文件路径过长"
    return None


class KnowledgeScanner:
    def __init__(self, settings, storage, documents):
        self.settings, self.storage, self.documents = settings, storage, documents

    def process_object(self, db, project, prefix, item):
        key = item["Key"]
        if not key.startswith(prefix):
            raise ValueError("Object outside project prefix")
        relative = key[len(prefix) :]
        reason = ignored_reason(relative, item.get("Size", 0), self.settings.max_upload_bytes)
        if reason:
            return "skipped", reason
        logical_key = "minio:" + relative
        deleted = db.scalar(
            select(Document.id).where(
                Document.project_id == project.id,
                Document.logical_key == logical_key,
                Document.is_deleted.is_(True),
            )
        )
        if deleted:
            return "skipped", "文档已在知识库中删除"
        # Re-read content: ETag is not a content digest. Upload computes SHA-256,
        # copies to immutable storage and reuses already parsed versions.
        with self.storage.open(key) as stream:
            result = self.documents.upload(
                db,
                UploadDocumentCommand(
                    stream=stream,
                    filename=PurePosixPath(relative).name,
                    project=project.name,
                    project_id=project.id,
                    logical_key=logical_key,
                    document_type="business-document",
                    lifecycle_status="approved",
                ),
            )
        if result.inline_job_id:
            self.documents.process_ingestion_job(db, result.inline_job_id)
        version = db.get(DocumentVersion, result.response.version_id)
        db.refresh(version)
        if version.technical_status == "searchable":
            if result.response.duplicate and not result.inline_job_id:
                return "skipped", "内容未变化，已解析"
            return "imported", "已解析并建立索引"
        if version.technical_status in {"failed_final", "failed_retryable"}:
            raise RuntimeError("文档解析失败，可再次扫描重试")
        return "skipped", "已有入库任务正在处理，请查看文档状态"

    def run(self, db, scan_id):
        scan = db.get(KnowledgeScan, scan_id)
        if scan is None or scan.status != "running":
            return
        project = db.get(Project, scan.project_id)
        prefix = scan_prefix(scan.project_id)
        try:
            for item in self.storage.list_objects(prefix):
                db.refresh(scan)
                if scan.status != "running":
                    return
                scan.current_key = item["Key"]
                scan.updated_at = utcnow()
                db.commit()
                try:
                    outcome, detail = self.process_object(db, project, prefix, item)
                except Exception as exc:
                    db.rollback()
                    logger.exception("扫描文件失败 scan_id=%s key=%s", scan_id, item["Key"])
                    outcome, detail = (
                        "failed",
                        f"处理失败（{type(exc).__name__}），请重试或查看入库任务",
                    )
                scan.processed += 1
                setattr(scan, outcome, getattr(scan, outcome) + 1)
                # Keep a bounded record of issues; totals remain complete.
                if outcome == "failed" or (outcome == "skipped" and detail != "内容未变化，已解析"):
                    scan.details = (
                        scan.details + [{"key": item["Key"], "status": outcome, "detail": detail}]
                    )[-100:]
                scan.updated_at = utcnow()
                db.commit()
            scan.status = "completed_with_errors" if scan.failed else "completed"
            scan.current_key = None
        except Exception:
            db.rollback()
            logger.exception("知识库扫描中断 scan_id=%s", scan_id)
            scan.status = "failed"
            scan.error = "扫描中断，请检查 MinIO 连接后重试。"
        scan.updated_at = utcnow()
        db.commit()
