from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import Chunk, Document, DocumentVersion, Project
from app.integrations.qwen import ChatCallResult, Usage
from app.knowledge.evidence import Evidence
from app.services.answering import AnswerService, AnswerValidationError, validate_answer
from app.services.model_router import NoModelAvailable


def evidence() -> list[Evidence]:
    return [
        Evidence(
            chunk_id="chunk-1",
            document_id="doc-1",
            version_id="version-1",
            project_id="project-1",
            filename="requirements.docx",
            document_status="approved",
            document_type="requirement",
            content="订单取消必须由主管审批。The order status becomes CANCELLED.",
            heading_path="取消流程",
        )
    ]


def test_validates_exact_quote() -> None:
    result = validate_answer(
        {
            "status": "answered",
            "answer": "订单取消需要主管审批。这里是没有引用的额外内容。",
            "claims": [
                {
                    "text": "订单取消需要主管审批。",
                    "evidence": [{"id": "chunk-1", "quote": "订单取消必须由主管审批。"}],
                }
            ],
        },
        evidence(),
    )
    assert result.status == "answered"
    assert result.answer == "订单取消需要主管审批。"
    assert result.sources[0].chunk_id == "chunk-1"


def test_rejects_fabricated_quote() -> None:
    with pytest.raises(AnswerValidationError):
        validate_answer(
            {
                "status": "answered",
                "answer": "经理审批。",
                "claims": [
                    {
                        "text": "经理审批。",
                        "evidence": [{"id": "chunk-1", "quote": "经理必须审批。"}],
                    }
                ],
            },
            evidence(),
        )


def test_insufficient_evidence_needs_no_citation() -> None:
    result = validate_answer(
        {"status": "insufficient_evidence", "answer": "没有足够证据。", "claims": []},
        evidence(),
    )
    assert result.sources == []


def test_live_citation_gate_rejects_an_approved_version_before_current_cutover() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = AnswerService(
        SimpleNamespace(validate_citations_against_database=True),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    validated = validate_answer(
        {
            "status": "answered",
            "answer": "订单取消需要主管审批。",
            "claims": [
                {
                    "text": "订单取消需要主管审批。",
                    "evidence": [{"id": "chunk-1", "quote": "订单取消必须由主管审批。"}],
                }
            ],
        },
        evidence(),
    )
    with Session(engine) as db:
        project = Project(id="project-1", name="P")
        document = Document(
            id="doc-1",
            project_id=project.id,
            logical_key="rule",
            filename="requirements.docx",
            document_type="requirement",
        )
        version = DocumentVersion(
            id="version-1",
            document_id=document.id,
            sha256="a" * 64,
            storage_path="source.docx",
            lifecycle_status="approved",
            technical_status="searchable",
            is_current=False,
        )
        chunk = Chunk(
            id="chunk-1",
            version_id=version.id,
            ordinal=0,
            content=evidence()[0].content,
            content_hash="b" * 64,
            record_hash="c" * 64,
            token_count=10,
        )
        db.add_all([project, document, version, chunk])
        db.commit()

        with pytest.raises(AnswerValidationError):
            service._validate_live_sources(db, validated, evidence())
        version.is_current = True
        db.commit()
        service._validate_live_sources(db, validated, evidence())


def test_chat_api_strictly_abstains_when_all_qwen_models_are_unavailable() -> None:
    class FakeRetriever:
        def search(self, question: str, project_ids: list[str]) -> list[Evidence]:
            return evidence()

    class UnavailableRouter:
        def call(self, *args: object, **kwargs: object) -> None:
            raise NoModelAvailable("provider unavailable")

    service = AnswerService(
        SimpleNamespace(prompt_version="test-v1"),
        FakeRetriever(),  # type: ignore[arg-type]
        UnavailableRouter(),  # type: ignore[arg-type]
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        response = service.answer(
            db,
            question="订单如何取消？",
            project_ids=[],
            conversation_id=None,
            pinned_model=None,
        )

    assert response.status == "insufficient_evidence"
    assert response.claims == []
    assert response.sources == []
    assert "provider unavailable" not in response.answer
    assert "生成模型当前不可用" in response.answer


def test_complex_answer_degrades_to_plus_when_max_tier_is_unavailable() -> None:
    """Provider entitlement failures must not make grounded answers unusable."""

    class FakeRetriever:
        def search(self, question: str, project_ids: list[str]) -> list[Evidence]:
            return evidence()

    class MaxUnavailableRouter:
        def __init__(self) -> None:
            self.tiers: list[str] = []

        def call(self, *args: object, **kwargs: object) -> ChatCallResult:
            tier = str(kwargs["tier"])
            self.tiers.append(tier)
            if tier == "max":
                raise NoModelAvailable("max entitlement unavailable")
            return ChatCallResult(
                model_id="plus-fallback",
                request_id="request-1",
                content=(
                    '{"status":"answered","answer":"ignored",'
                    '"claims":[{"text":"订单取消需要主管审批。",'
                    '"evidence":[{"id":"chunk-1",'
                    '"quote":"订单取消必须由主管审批。"}]}]}'
                ),
                usage=Usage(),
                latency_ms=1,
            )

    router = MaxUnavailableRouter()
    service = AnswerService(
        SimpleNamespace(
            prompt_version="test-v1",
            validate_citations_against_database=False,
        ),
        FakeRetriever(),  # type: ignore[arg-type]
        router,  # type: ignore[arg-type]
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        response = service.answer(
            db,
            question="请比较订单取消规则。",
            project_ids=[],
            conversation_id=None,
            pinned_model=None,
        )

    assert router.tiers == ["max", "plus"]
    assert response.status == "answered"
    assert response.model_id == "plus-fallback"
    assert response.route_tier == "plus"
    assert response.sources[0].chunk_id == "chunk-1"
