from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.answering import (
    SYSTEM_PROMPT,
    AnswerService,
    AnswerValidationError,
    attach_cross_document_bridges,
    validate_answer,
)
from app.db import Base
from app.model_router import NoModelAvailable
from app.models import Chunk, Document, DocumentVersion, Project
from app.qwen import ChatCallResult, Usage
from app.search import Evidence


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


def test_expands_ellipsis_to_exact_contiguous_source_quote() -> None:
    result = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "订单状态会变更。",
                    "evidence": [
                        {
                            "id": "chunk-1",
                            "quote": "订单取消必须由主管审批。...The order status becomes CANCELLED",
                        }
                    ],
                }
            ],
        },
        evidence(),
    )
    assert "The order status becomes CANCELLED" in result.sources[0].quote


def test_repairs_table_punctuation_to_an_exact_source_span() -> None:
    item = evidence()[0]
    item.content = "业务名称 | 雪松门 | 控制编号：CTL-4616"
    result = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "雪松门的控制编号是 CTL-4616。",
                    "evidence": [
                        {
                            "id": "chunk-1",
                            "quote": "业务名称：雪松门，控制编号: CTL-4616",
                        }
                    ],
                }
            ],
        },
        [item],
    )
    assert result.sources[0].quote == "业务名称 | 雪松门 | 控制编号：CTL-4616"


def test_formatting_repair_does_not_accept_changed_words() -> None:
    item = evidence()[0]
    item.content = "业务名称 | 雪松门 | 控制编号：CTL-4616"
    with pytest.raises(AnswerValidationError):
        validate_answer(
            {
                "status": "answered",
                "answer": "ignored",
                "claims": [
                    {
                        "text": "错误结论",
                        "evidence": [
                            {
                                "id": "chunk-1",
                                "quote": "业务名称：雪松门，控制编号: CTL-9999",
                            }
                        ],
                    }
                ],
            },
            [item],
        )


def test_repairs_translated_connectives_around_unchanged_business_anchors() -> None:
    item = evidence()[0]
    item.content = (
        "English operational name / 英文运行称谓: Greenlake Credit\n"
        "Chinese control name / 中文控制规则: 雪松门\n"
        "control reference / 控制引用: CTL-4616"
    )
    result = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "Greenlake Credit maps to CTL-4616.",
                    "evidence": [
                        {
                            "id": "chunk-1",
                            "quote": "Greenlake Credit maps to control reference CTL-4616.",
                        }
                    ],
                }
            ],
        },
        [item],
    )
    assert "Greenlake Credit" in result.sources[0].quote
    assert "CTL-4616" in result.sources[0].quote
    assert result.sources[0].quote in item.content


def test_anchored_repair_rejects_a_changed_identifier() -> None:
    item = evidence()[0]
    item.content = "Greenlake Credit | control reference CTL-4616"
    with pytest.raises(AnswerValidationError):
        validate_answer(
            {
                "status": "answered",
                "answer": "ignored",
                "claims": [
                    {
                        "text": "wrong",
                        "evidence": [
                            {
                                "id": "chunk-1",
                                "quote": "Greenlake Credit maps to CTL-9999",
                            }
                        ],
                    }
                ],
            },
            [item],
        )


def test_insufficient_evidence_needs_no_citation() -> None:
    result = validate_answer(
        {"status": "insufficient_evidence", "answer": "没有足够证据。", "claims": []},
        evidence(),
    )
    assert result.sources == []


def test_retry_adds_exact_quote_and_cross_document_correction() -> None:
    assert "bridge evidence" in SYSTEM_PROMPT

    class FakeRetriever:
        def search(self, question: str, project_ids: list[str]) -> list[Evidence]:
            return evidence()

    class CorrectingRouter:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def call(self, *args: object, **kwargs: object) -> ChatCallResult:
            self.prompts.append(str(kwargs["user_prompt"]))
            quote = "not present" if len(self.prompts) == 1 else "订单取消必须由主管审批。"
            return ChatCallResult(
                model_id="test-model",
                request_id=f"request-{len(self.prompts)}",
                content=(
                    '{"status":"answered","answer":"ignored",'
                    '"claims":[{"text":"订单取消需要主管审批。",'
                    f'"evidence":[{{"id":"chunk-1","quote":"{quote}"}}]}}]}}'
                ),
                usage=Usage(),
                latency_ms=1,
            )

    router = CorrectingRouter()
    service = AnswerService(
        SimpleNamespace(prompt_version="test-v1", validate_citations_against_database=False),
        FakeRetriever(),  # type: ignore[arg-type]
        router,  # type: ignore[arg-type]
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        response = service.answer(
            db,
            question="订单如何取消？",
            project_ids=[],
            conversation_id=None,
            pinned_model="test-model",
        )
    assert response.status == "answered"
    assert len(router.prompts) == 2
    assert "PREVIOUS_OUTPUT_REJECTED" in router.prompts[1]
    assert "at most 6 claims" in router.prompts[1]


def test_server_adds_subject_to_downstream_provenance_bridge() -> None:
    items = [
        Evidence(
            chunk_id="bridge",
            document_id="requirement",
            version_id="v1",
            project_id="p1",
            filename="01-BIZ-1201-business-requirement.md",
            document_status="approved",
            document_type="requirement",
            content="API 路径与拒绝码从 RTE-6101 接口规范获取。",
        ),
        Evidence(
            chunk_id="api",
            document_id="api-doc",
            version_id="v2",
            project_id="p1",
            filename="routing-api.html",
            document_status="approved",
            document_type="api",
            content="Validation rejection: E-7101",
            heading_path="RTE-6101 Authorization operation",
        ),
    ]
    validated = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "拒绝码为 E-7101。",
                    "evidence": [{"id": "api", "quote": "E-7101"}],
                }
            ],
        },
        items,
    )
    result = attach_cross_document_bridges("BIZ-1201 的拒绝码是什么？", validated, items)
    assert result.claims[0].citations == ["api", "bridge"]
    assert {source.filename for source in result.sources} == {
        "routing-api.html",
        "01-BIZ-1201-business-requirement.md",
    }


def test_server_bridges_chinese_business_subject_without_explicit_id() -> None:
    items = [
        Evidence(
            chunk_id="bridge",
            document_id="requirement",
            version_id="v1",
            project_id="p1",
            filename="requirement.md",
            document_status="approved",
            document_type="requirement",
            content="雾桥结算引擎 的治理策略引用 POL-4101。",
        ),
        Evidence(
            chunk_id="policy",
            document_id="policy-doc",
            version_id="v2",
            project_id="p1",
            filename="policy.docx",
            document_status="approved",
            document_type="architecture",
            content="Policy reference | POL-4101",
        ),
    ]
    validated = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "当前策略为 POL-4101。",
                    "evidence": [{"id": "policy", "quote": "POL-4101"}],
                }
            ],
        },
        items,
    )
    result = attach_cross_document_bridges(
        "雾桥结算引擎 当前受哪个策略管控？", validated, items
    )
    assert result.claims[0].citations == ["policy", "bridge"]


def test_server_bridges_english_alias_through_multilingual_registry() -> None:
    items = [
        Evidence(
            chunk_id="registry",
            document_id="registry-doc",
            version_id="v1",
            project_id="p1",
            filename="9f6c3a012bed.txt",
            document_status="approved",
            document_type="terminology-registry",
            content=(
                "Chinese business name: 岚桥清算\n"
                "English operational name: Mistbridge Clearing\n"
                "control reference: CTL-4601"
            ),
        ),
        Evidence(
            chunk_id="control",
            document_id="control-doc",
            version_id="v2",
            project_id="p1",
            filename="15db2ca9be31.docx",
            document_status="approved",
            document_type="runtime-control",
            content="Control reference CTL-4601 retains evidence for 376 days.",
        ),
    ]
    validated = validate_answer(
        {
            "status": "answered",
            "answer": "ignored",
            "claims": [
                {
                    "text": "Audit evidence is retained for 376 days.",
                    "evidence": [
                        {
                            "id": "control",
                            "quote": "Control reference CTL-4601 retains evidence for 376 days.",
                        }
                    ],
                }
            ],
        },
        items,
    )
    result = attach_cross_document_bridges(
        "For Mistbridge Clearing, how long is audit evidence retained?", validated, items
    )
    assert result.claims[0].citations == ["control", "registry"]


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
