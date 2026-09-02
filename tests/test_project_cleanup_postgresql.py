from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.application.document_service import DocumentApplicationService
from app.core.config import Settings
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentArtifact,
    DocumentVersion,
    EmbeddingCache,
    EmbeddingModel,
)
from app.integrations.search import SearchIndex
from app.integrations.storage import LocalObjectStorage
from app.repositories.documents import DocumentRepository


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to an isolated migrated PostgreSQL database",
)
class PostgreSQLProjectCleanupTest(unittest.TestCase):
    """在真实外键和 pgvector 搜索表上验证知识库物理清理。"""

    def test_cleanup_cascades_search_rows_and_preserves_shared_objects(self) -> None:
        database_url = os.environ["TEST_DATABASE_URL"]
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _env_file=None,
                database_url=database_url,
                storage_root=Path(temp_dir),
            )
            engine = create_engine(database_url)
            index = SearchIndex(settings, engine=engine)
            storage = LocalObjectStorage(settings.storage_root)
            service = DocumentApplicationService(
                settings,
                storage,
                DocumentRepository(),
                ingestion=None,
            )
            shared_source, _, _ = storage.save_bytes("sources/shared.txt", b"shared")
            artifact_path, _, _ = storage.save_bytes("artifacts/target.json", b"{}")
            vector = [1.0, *([0.0] * (settings.embedding_dimensions - 1))]
            vector_path, checksum, _ = storage.save_vector(
                settings.embedding_fingerprint,
                "a" * 64,
                vector,
            )

            with Session(engine, expire_on_commit=False) as db:
                target = service.create_project(db, "pg-cleanup-target")
                retained = service.create_project(db, "pg-cleanup-retained")
                target_document = Document(
                    project_id=target.id,
                    logical_key="target.txt",
                    filename="target.txt",
                    document_type="text-document",
                    is_deleted=True,
                )
                retained_document = Document(
                    project_id=retained.id,
                    logical_key="retained.txt",
                    filename="retained.txt",
                    document_type="text-document",
                )
                db.add_all([target_document, retained_document])
                db.flush()
                target_version = DocumentVersion(
                    document_id=target_document.id,
                    sha256="b" * 64,
                    storage_path=shared_source,
                    lifecycle_status="approved",
                    technical_status="searchable",
                    is_current=True,
                )
                retained_version = DocumentVersion(
                    document_id=retained_document.id,
                    sha256="c" * 64,
                    storage_path=shared_source,
                    lifecycle_status="approved",
                    technical_status="searchable",
                    is_current=True,
                )
                db.add_all([target_version, retained_version])
                db.flush()
                target_chunk = Chunk(
                    version_id=target_version.id,
                    ordinal=0,
                    content="target",
                    content_hash="a" * 64,
                    record_hash="d" * 64,
                    token_count=1,
                )
                retained_chunk = Chunk(
                    version_id=retained_version.id,
                    ordinal=0,
                    content="retained",
                    content_hash="a" * 64,
                    record_hash="e" * 64,
                    token_count=1,
                )
                model = EmbeddingModel(
                    fingerprint=settings.embedding_fingerprint,
                    model_id=settings.embedding_model,
                    dimensions=settings.embedding_dimensions,
                    preprocess_version=settings.embedding_preprocess_version,
                )
                db.add_all([target_chunk, retained_chunk, model])
                db.flush()
                cache = EmbeddingCache(
                    content_hash="a" * 64,
                    embedding_fingerprint=model.fingerprint,
                    object_uri=vector_path,
                    checksum=checksum,
                    dimensions=settings.embedding_dimensions,
                )
                artifact = DocumentArtifact(
                    version_id=target_version.id,
                    artifact_type="normalized",
                    object_uri=artifact_path,
                    sha256="f" * 64,
                    byte_size=2,
                    fingerprint="test-parser",
                )
                db.add_all([cache, artifact])
                db.flush()
                db.add_all(
                    [
                        ChunkEmbedding(
                            chunk_id=target_chunk.id,
                            embedding_fingerprint=model.fingerprint,
                            cache_id=cache.id,
                        ),
                        ChunkEmbedding(
                            chunk_id=retained_chunk.id,
                            embedding_fingerprint=model.fingerprint,
                            cache_id=cache.id,
                        ),
                    ]
                )
                db.commit()

                index.index_chunks(
                    [
                        {
                            "chunk_id": target_chunk.id,
                            "filename": "target.txt",
                            "content": "target",
                            "embedding": vector,
                        },
                        {
                            "chunk_id": retained_chunk.id,
                            "filename": "retained.txt",
                            "content": "retained",
                            "embedding": vector,
                        },
                    ]
                )
                self.assertEqual(index.count_all(), 2)

                cleared = service.cleanup_project(db, target.id)

                self.assertEqual(cleared.documents_deleted, 1)
                self.assertEqual(index.count_all(), 1)
                self.assertTrue(storage.resolve(shared_source).exists())
                self.assertTrue(storage.resolve(vector_path).exists())
                self.assertFalse(storage.resolve(artifact_path).exists())

                deleted = service.delete_project(db, retained.id)

                self.assertTrue(deleted.project_deleted)
                self.assertEqual(index.count_all(), 0)
                self.assertFalse(storage.resolve(shared_source).exists())
                self.assertFalse(storage.resolve(vector_path).exists())
            index.close()
