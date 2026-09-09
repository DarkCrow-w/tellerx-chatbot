"""Protect transaction boundaries and answer failure paths during service refactoring."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.schemas import ClaimOut
from app.core.config import Settings
from app.db import Base
from app.db.models import (
    Chunk,
    Document,
    DocumentSection,
    DocumentVersion,
    IngestionJob,
    OutboxEvent,
    Project,
)
from app.knowledge.chunking import TextChunk
from app.knowledge.evidence import Evidence
from app.services.answer_bridges import attach_cross_document_bridges
from app.services.answer_contract import ValidatedAnswer
from app.services.answering import AnswerPreparation, AnswerService, GenerationResult
from app.services.ingestion import IngestionService
from app.services.query_understanding import fallback_query_plan


def evidence(chunk_id, content, document_type="architecture"):
    return Evidence(
        chunk_id, chunk_id, "version", "project", "青禾清算.md", "approved", document_type, content
    )


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("ambiguous_document", "clarification_required"),
        ("document_not_found", "insufficient_evidence"),
        (None, "insufficient_evidence"),
    ],
)
def test_no_evidence_persists_once_without_generation(failure, expected):
    service = AnswerService(Settings(_env_file=None), Mock(), Mock(), repository=Mock())
    service._prepare_answer = Mock(
        return_value=AnswerPreparation(
            fallback_query_plan("青禾清算", "test"),
            [],
            None,
            None,
            failure_reason=failure,
        )
    )
    service._generate_answer = Mock()
    service._persist_response = Mock(side_effect=lambda db, **kwargs: kwargs["validated"])
    result = service.answer(
        Mock(), question="青禾清算", project_ids=[], conversation_id=None, pinned_model=None
    )
    assert result.status == expected
    service._generate_answer.assert_not_called()
    service._persist_response.assert_called_once()


@pytest.mark.parametrize("failure", ["validation", "provider", None])
def test_generated_or_failed_answer_keeps_model_and_persists_once(failure):
    service = AnswerService(Settings(_env_file=None), Mock(), Mock(), repository=Mock())
    service._prepare_answer = Mock(
        return_value=AnswerPreparation(
            fallback_query_plan("青禾清算", "test"),
            [evidence("a", "正文")],
            "plus",
            "prompt",
        )
    )
    valid = ValidatedAnswer("answered", "答案", [], []) if failure is None else None
    service._generate_answer = Mock(
        return_value=GenerationResult(valid, "model", "plus", failure or "validation")
    )
    service._persist_response = Mock(side_effect=lambda db, **kwargs: kwargs)
    result = service.answer(
        Mock(), question="青禾清算", project_ids=[], conversation_id=None, pinned_model="model"
    )
    assert result["model_id"] == "model"
    assert result["actual_tier"] == "plus"
    assert result["validated"].status == ("answered" if valid else "insufficient_evidence")
    if valid:
        assert result["validated"] is valid
    service._persist_response.assert_called_once()


def test_bridge_prefers_mapping_and_never_rewrites_claim():
    downstream = evidence("value", "DLQ-8802 达到 77 条告警。")
    mapping = evidence("mapping", "业务说明。青禾清算使用 DLQ-8802。", "mapping")
    other = evidence("other", "青禾清算使用 DLQ-8802。")
    answer = ValidatedAnswer(
        "answered", "77 条告警。", [ClaimOut(text="77 条告警。", citations=["value"])], []
    )
    result = attach_cross_document_bridges(
        "青禾清算的告警阈值", answer, [downstream, other, mapping]
    )
    assert result.claims[0].text == "77 条告警。"
    assert result.claims[0].citations == ["value", "mapping"]
    assert result.sources[0].quote == "青禾清算使用 DLQ-8802。"


def test_ingestion_retry_preserves_sections_neighbors_and_outbox_transaction():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = IngestionService(
        Settings(_env_file=None), Mock(), Mock(), Mock(), indexer=Mock(), storage=Mock()
    )
    with Session(engine) as db:
        project = Project(name="refactor-test")
        document = Document(
            project=project,
            logical_key="sample.md",
            filename="sample.md",
            document_type="text-document",
        )
        version = DocumentVersion(
            document=document,
            sha256="a" * 64,
            storage_path="sample.md",
            lifecycle_status="approved",
        )
        db.add(version)
        db.flush()
        job = IngestionJob(document_id=document.id, version_id=version.id)
        db.add(job)
        db.commit()
        chunks = [
            TextChunk(
                0,
                "父章节",
                "a" * 64,
                3,
                "架构",
                1,
                None,
                None,
                section_key="parent",
                section_level=1,
            ),
            TextChunk(
                1,
                "父章节续段",
                "b" * 64,
                5,
                "架构",
                2,
                None,
                None,
                section_key="parent",
                section_level=1,
            ),
            TextChunk(
                2,
                "子章节",
                "c" * 64,
                3,
                "架构 > 接口",
                3,
                None,
                None,
                section_key="child",
                parent_section_key="parent",
                section_level=2,
            ),
        ]
        first_ids = None
        for _ in range(2):
            event_id = service._persist_chunks_and_event(db, job, version, chunks, {}, ["warning"])
            rows = list(
                db.scalars(
                    select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal)
                )
            )
            assert len(rows) == 3
            ids = [row.id for row in rows]
            if first_ids is not None:
                assert ids == first_ids
            first_ids = ids
            assert rows[0].next_chunk_id == rows[1].id
            assert rows[1].previous_chunk_id == rows[0].id
            assert rows[1].parent_chunk_id == rows[0].id
            assert rows[2].parent_chunk_id == rows[0].id
            sections = {row.section_key: row for row in db.scalars(select(DocumentSection))}
            assert set(sections) == {"root", "parent", "child"}
            assert sections["child"].parent_section_id == sections["parent"].id
            assert sections["root"].page_start == 1 and sections["root"].page_end == 3
            assert db.get(OutboxEvent, event_id).aggregate_id == version.id
            assert job.status == version.technical_status == "index_pending"
            assert job.warnings == ["warning"]
    engine.dispose()


@pytest.mark.parametrize("empty_document", [False, True])
def test_ingestion_process_saves_success_or_failure_stage(empty_document, caplog):
    from app.knowledge.chunking import ParsedUnit

    caplog.set_level("INFO", logger="app.services.ingestion")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    parser = Mock()
    parser.parse.return_value = ([] if empty_document else [ParsedUnit("接口规则正文。")], [])
    service = IngestionService(
        Settings(_env_file=None), parser, Mock(), Mock(), indexer=Mock(), storage=Mock()
    )
    service._save_normalized_artifact = Mock()
    service._embeddings = Mock(return_value={})
    with Session(engine) as db:
        project = Project(name="process-test")
        document = Document(
            project=project,
            logical_key="process.md",
            filename="process.md",
            document_type="text-document",
        )
        version = DocumentVersion(
            document=document,
            sha256="b" * 64,
            storage_path="process.md",
            lifecycle_status="approved",
        )
        db.add(version)
        db.flush()
        job = IngestionJob(document_id=document.id, version_id=version.id)
        db.add(job)
        db.commit()
        if empty_document:
            with pytest.raises(ValueError, match="Parser produced no chunks"):
                service.process(db, job.id)
            assert job.status == "failed"
            assert version.technical_status == "failed_final"
            service._embeddings.assert_not_called()
            assert "stage=parsing" in caplog.text
            assert "入库任务失败" in caplog.text
        else:
            event_id = service.process(db, job.id)
            assert db.get(OutboxEvent, event_id).event_type == "index_version"
            assert job.status == "index_pending"
            assert len(list(db.scalars(select(Chunk)))) == 1
            service._save_normalized_artifact.assert_called_once()
            assert "文档切块开始" in caplog.text
            assert "min_chunk_tokens=" in caplog.text
            assert "max_chunk_tokens=" in caplog.text
            assert "elapsed_ms=" in caplog.text
            assert "接口规则正文" not in caplog.text
    engine.dispose()


def test_reranking_restores_identifier_omitted_by_model():
    from dataclasses import asdict

    from app.services.retrieval import Retriever

    settings = Settings(_env_file=None, rerank_enabled=True)
    model = Mock()
    model.rerank.return_value = [(1, 0.9)]
    retriever = Retriever(settings, Mock(), model)
    wanted = evidence("wanted", "DLQ-8802 阈值 77 条。")
    unrelated = evidence("unrelated", "其他业务说明。")
    candidates = [{"hit": {"_source": asdict(item)}, "score": 0.1} for item in [wanted, unrelated]]
    result = retriever._rank_candidates(
        query="DLQ-8802",
        candidates=candidates,
        related_hits=[],
        linked_identifiers=[],
        semantic_context="",
        semantic_anchors=(),
    )
    assert "wanted" in {item.chunk_id for item in result}
    model.rerank.assert_called_once()
