"""Manual end-to-end check: requires configured MinIO, database and model service.

Creates and removes an isolated validation project; model usage audit remains.
"""

import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Chunk, DocumentVersion, Project
from app.db.session import SessionLocal
from app.main import app

with TestClient(app) as client:
    ready = client.get("/health/ready")
    assert ready.status_code == 200, ready.text
    with SessionLocal() as db:
        versions = list(db.scalars(select(DocumentVersion)))
        for version in versions:
            r = client.get(
                f"/api/v1/documents/{version.document_id}/download",
                params={"version_id": version.id},
            )
            assert r.status_code == 200, r.text[:100]
            assert hashlib.sha256(r.content).hexdigest() == version.sha256
        ids = list(db.scalars(select(Chunk.id).limit(3)))
        for id in ids:
            assert client.get(f"/api/v1/sources/{id}").status_code == 200
    print("Readiness, all six old version downloads and source citations PASS")
    project = "minio-validation-" + uuid.uuid4().hex[:10]
    payload = (
        "# MinIO 验证\n\n验证编号是 MINIO-7319。验证文件用于检查对象存储上传和解析。\n".encode()
    )
    try:
        result = client.post(
            "/api/v1/documents",
            data={
                "project": project,
                "document_type": "text-document",
                "lifecycle_status": "approved",
            },
            files={"file": ("中文验证.md", payload, "text/markdown")},
        )
        assert result.status_code == 202, result.text
        response = result.json()
        with SessionLocal() as db:
            version = db.get(DocumentVersion, response["version_id"])
            assert version.technical_status == "searchable", version.technical_status
            assert list(db.scalars(select(Chunk.id).where(Chunk.version_id == version.id)))
            project_id = db.scalar(select(Project.id).where(Project.name == project))
        download = client.get(f"/api/v1/documents/{response['document_id']}/download")
        assert download.content == payload
        answer = client.post(
            "/api/v1/chat", json={"question": "验证编号是什么？", "project_ids": [project_id]}
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["status"] == "answered"
        assert answer.json()["sources"]
        assert "MINIO-7319" in answer.json()["answer"]
        print("New Chinese filename upload, ingestion, download and cited chat PASS")
    finally:
        with SessionLocal() as db:
            project_id = db.scalar(select(Project.id).where(Project.name == project))
        if project_id:
            result = client.delete("/api/v1/projects/" + project_id)
            assert result.status_code == 200, result.text
            print("Isolated validation project cleaned up")
