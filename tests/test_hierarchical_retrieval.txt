"""Focused regression tests for document scope and hierarchical metadata."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import Base
from app.db.models import Chunk, Document, DocumentSection, DocumentVersion, Project
from app.knowledge.chunking import TextChunk
from app.knowledge.document_scope import normalize_document_name, score_document_name
from app.knowledge.parsers import DocumentParser
from app.repositories.documents import DocumentRepository
from app.services.answering import AnswerService
from app.services.ingestion import IngestionService
from app.services.query_understanding import _semantic_plan, fallback_query_plan
from app.services.retrieval import RetrievalOutcome, Retriever


class DocumentScopeTest(unittest.TestCase):
    def test_filename_normalization_is_bounded_and_explainable(self) -> None:
        self.assertEqual(
            normalize_document_name("支付平台（二期）_架构-V2.DOCX"), "支付平台 二期 架构 v2"
        )
        self.assertGreater(score_document_name("支付平台二期", "支付平台（二期）架构.docx"), 0.7)
        self.assertEqual(score_document_name("结算", "支付平台（二期）架构.docx"), 0.0)
        self.assertEqual(score_document_name("文档", "支付平台文档.docx"), 0.0)

    def test_natural_document_scope_examples(self) -> None:
        cases = {
            "支付平台架构里的鉴权方式是什么？": ("支付平台架构", "鉴权方式是什么?", ()),
            "帮我看看支付平台二期这份文档，接口失败怎么处理？": (
                "支付平台二期",
                "接口失败怎么处理?",
                (),
            ),
            "支付平台二期的接口模块里怎么定义签名？": (
                "支付平台二期",
                "怎么定义签名?",
                ("接口模块",),
            ),
            "项目 A 架构文档中，接口模块对重试次数有什么要求？": (
                "项目 A 架构",
                "接口模块对重试次数有什么要求?",
                ("接口模块",),
            ),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                plan = fallback_query_plan(question, "test")
                self.assertEqual(
                    (plan.document_hint, plan.document_question, plan.section_hints), expected
                )
                self.assertEqual(plan.retrieval_intent, "document_lookup")
        global_plan = fallback_query_plan("系统出现鉴权失败时应该怎么处理？", "test")
        self.assertEqual(global_plan.retrieval_intent, "global_lookup")
        self.assertIsNone(global_plan.document_hint)

    def test_model_cannot_turn_a_plain_business_question_into_hard_scope(self) -> None:
        plan = _semantic_plan(
            "鉴权失败怎么处理？",
            {
                "subjects": ["鉴权"],
                "document_hint": "鉴权失败",
                "document_question": "怎么处理",
                "retrieval_intent": "document_lookup",
            },
            "test-model",
        )
        self.assertIsNone(plan.document_hint)
        self.assertEqual(plan.retrieval_intent, "global_lookup")

    def test_markdown_parser_preserves_heading_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.md"
            path.write_text("# 架构模块\n总览。\n## 接口模块\n签名规则。", encoding="utf-8")
            units, warnings = DocumentParser().parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual(units[0].section_path, ("架构模块",))
        self.assertEqual(units[1].section_path, ("架构模块", "接口模块"))
        self.assertEqual(units[1].parent_section_key, units[0].section_key)

    def test_contextual_embedding_identity_changes_with_document(self) -> None:
        def chunk() -> TextChunk:
            return TextChunk(0, "相同模板正文", "hash", 6, "接口模块", None, None, None)

        first, second = [chunk()], [chunk()]
        IngestionService._prepare_embedding_inputs(
            first, Document(filename="项目A架构.docx", document_type="architecture")
        )
        IngestionService._prepare_embedding_inputs(
            second, Document(filename="项目B架构.docx", document_type="architecture")
        )
        self.assertNotEqual(first[0].embedding_input_hash, second[0].embedding_input_hash)
        self.assertIn("项目A架构.docx", first[0].embedding_input or "")

    def test_thirty_project_filename_fixture_keeps_expected_top_one(self) -> None:
        """模拟 30 个目录一致项目，名称信号仍能区分目标文档。"""

        for project_number in range(1, 31):
            prefix = f"清算项目{project_number}"
            filenames = [
                f"{prefix}架构方案.docx",
                f"{prefix}接口说明.docx",
                f"{prefix}功能说明.docx",
                f"{prefix}运维手册.docx",
            ]
            hint = f"{prefix}接口"
            ranked = sorted(
                filenames,
                key=lambda filename: score_document_name(hint, filename),
                reverse=True,
            )
            self.assertEqual(ranked[0], filenames[1])


class RetrievalScopeDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = Mock()
        self.model = Mock()
        self.retriever = Retriever(Settings(), self.index, self.model)

    def test_ambiguous_filename_requests_clarification_without_searching_content(self) -> None:
        self.index.document_candidates.return_value = [
            {"document_id": "a", "filename": "支付平台二期架构.docx", "score": 0.86},
            {"document_id": "b", "filename": "支付平台二期接口.docx", "score": 0.84},
        ]
        result = self.retriever.search_with_scope(
            "支付平台二期文档里怎么签名？",
            ["project"],
            query_plan=fallback_query_plan("支付平台二期文档里怎么签名？", "test"),
        )
        self.assertEqual(result.resolved_scope, "clarification_required")
        self.assertEqual(len(result.clarification_options), 2)
        self.index.lexical_search.assert_not_called()

    def test_unique_filename_hard_scopes_every_retrieval_call(self) -> None:
        self.index.document_candidates.return_value = [
            {"document_id": "a", "filename": "支付平台二期架构.docx", "score": 0.9}
        ]
        self.index.lexical_search.return_value = []
        self.index.vector_search.return_value = []
        self.model.embeddings.side_effect = RuntimeError("offline")
        result = self.retriever.search_with_scope(
            "支付平台二期文档里怎么签名？",
            ["project"],
            query_plan=fallback_query_plan("支付平台二期文档里怎么签名？", "test"),
        )
        self.assertEqual(result.resolved_scope, "支付平台二期架构.docx")
        self.assertEqual(result.resolved_document["document_id"], "a")
        self.assertTrue(self.index.lexical_search.called)
        for call in self.index.lexical_search.call_args_list:
            self.assertEqual(call.kwargs["document_ids"], ["a"])

    def test_explicit_missing_document_never_falls_back_to_global_content(self) -> None:
        self.index.document_candidates.return_value = []
        result = self.retriever.search_with_scope(
            "不存在项目文档里怎么签名？",
            ["project"],
            query_plan=fallback_query_plan("不存在项目文档里怎么签名？", "test"),
        )
        self.assertEqual(result.failure_reason, "document_not_found")
        self.index.lexical_search.assert_not_called()

    def test_no_document_hint_keeps_global_search_path(self) -> None:
        self.index.lexical_search.return_value = []
        self.index.vector_search.return_value = []
        self.model.embeddings.side_effect = RuntimeError("offline")
        result = self.retriever.search_with_scope(
            "鉴权失败怎么处理？",
            ["project"],
            query_plan=fallback_query_plan("鉴权失败怎么处理？", "test"),
        )
        self.assertEqual(result.resolved_scope, "global")
        self.index.document_candidates.assert_not_called()
        for call in self.index.lexical_search.call_args_list:
            self.assertNotIn("document_ids", call.kwargs)

    def test_cross_source_intent_is_preserved_without_a_document_filter(self) -> None:
        self.index.lexical_search.return_value = []
        self.index.vector_search.return_value = []
        self.model.embeddings.side_effect = RuntimeError("offline")
        plan = replace(
            fallback_query_plan("比较两个系统的超时规则", "test"),
            retrieval_intent="cross_source",
        )
        result = self.retriever.search_with_scope(
            "比较两个系统的超时规则", ["project"], query_plan=plan
        )
        self.assertEqual(result.retrieval_intent, "cross_source")


class AnswerScopeContractTest(unittest.TestCase):
    def test_ambiguous_scope_is_persisted_without_calling_generation_model(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        outcome = Mock(spec=["search_with_scope"])
        outcome.search_with_scope.return_value = RetrievalOutcome(
            retrieval_intent="ambiguous",
            resolved_scope="clarification_required",
            retrieval_confidence=0.86,
            clarification_options=[
                {
                    "document_id": "document-a",
                    "project_id": "project-a",
                    "filename": "支付平台二期架构.docx",
                    "version_id": "version-a",
                    "document_type": "architecture",
                    "version_label": "2.0",
                    "score": 0.86,
                }
            ],
            failure_reason="ambiguous_document",
        )
        router = Mock()
        with Session(engine) as db:
            response = AnswerService(Settings(), outcome, router).answer(
                db,
                question="支付平台二期文档里怎么签名？",
                project_ids=["project-a"],
                conversation_id=None,
                pinned_model=None,
            )
        engine.dispose()
        self.assertEqual(response.status, "clarification_required")
        self.assertEqual(response.resolved_scope, "clarification_required")
        self.assertEqual(response.clarification_options[0].document_id, "document-a")
        router.call.assert_not_called()

    def test_section_context_includes_immediate_neighbor(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            project = Project(id="project", name="项目")
            document = Document(
                id="document",
                project_id="project",
                logical_key="architecture",
                filename="架构.docx",
                normalized_filename="架构",
                document_type="architecture",
            )
            version = DocumentVersion(
                id="version",
                document_id="document",
                sha256="a" * 64,
                storage_path="source.docx",
                lifecycle_status="approved",
                technical_status="searchable",
                is_current=True,
            )
            first = DocumentSection(
                id="section-1",
                version_id="version",
                section_key="first",
                level=1,
                title="接口模块",
                normalized_title="接口模块",
                heading_path="接口模块",
                ordinal=1,
            )
            second = DocumentSection(
                id="section-2",
                version_id="version",
                section_key="second",
                level=1,
                title="运维模块",
                normalized_title="运维模块",
                heading_path="运维模块",
                ordinal=2,
            )
            db.add_all([project, document, version, first, second])
            db.flush()
            for ordinal, section_id in enumerate(("section-1", "section-1", "section-2")):
                db.add(
                    Chunk(
                        id=f"chunk-{ordinal}",
                        version_id="version",
                        section_id=section_id,
                        ordinal=ordinal,
                        content=f"content-{ordinal}",
                        content_hash=str(ordinal) * 64,
                        embedding_input_hash=str(ordinal) * 64,
                        record_hash=str(ordinal) * 64,
                        token_count=1,
                    )
                )
            db.commit()
            section, chunks, truncated = DocumentRepository().get_section_context(
                db, section_id="section-1"
            )
            self.assertEqual(section.id, "section-1")
            self.assertEqual([chunk.id for chunk in chunks], ["chunk-0", "chunk-1", "chunk-2"])
            self.assertFalse(truncated)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
