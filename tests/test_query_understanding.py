import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.query_understanding import QueryUnderstandingService, fallback_query_plan
from app.qwen import ChatCallResult, Usage


class PlanningRouter:
    def __init__(self, payload: dict | str):
        self.payload = payload
        self.calls: list[dict] = []

    def call(self, *args: object, **kwargs: object) -> ChatCallResult:
        self.calls.append(kwargs)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return ChatCallResult(
            model_id="plus-planner",
            request_id="plan-1",
            content=content,
            usage=Usage(),
            latency_ms=1,
        )


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        semantic_query_understanding_enabled=True,
        query_plan_cache_size=50,
        query_plan_cache_ttl_seconds=60,
    )


def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_semantic_plan_extracts_keys_from_indirect_multi_part_wording() -> None:
    router = PlanningRouter(
        {
            "language": "zh",
            "intent": "lookup",
            "subjects": ["翠湖授信", "雪松门"],
            "identifiers": [],
            "requested_facts": ["审批角色", "审批阈值", "超时时间"],
            "constraints": ["亚太北区", "高金额授权"],
            "retrieval_queries": [
                "翠湖授信 亚太北区 高金额授权 审批角色 审批阈值",
                "雪松门 高金额授权 超时时间",
            ],
        }
    )
    service = QueryUnderstandingService(settings(), router)  # type: ignore[arg-type]
    with session() as db:
        plan = service.understand(
            db,
            "我这边要把那套翠湖授信放到亚太北区跑大额操作，最后谁点头、卡多少钱、等多久算超时？",
        )
    assert plan.strategy == "semantic-qwen-v1"
    assert plan.subjects == ("翠湖授信", "雪松门")
    assert plan.requested_facts == ("审批角色", "审批阈值", "超时时间")
    assert plan.constraints == ("亚太北区", "高金额授权")
    assert len(plan.retrieval_queries) >= 2
    assert router.calls[0]["max_tokens"] == 700


def test_model_cannot_invent_identifiers() -> None:
    router = PlanningRouter(
        {
            "language": "zh",
            "intent": "lookup",
            "subjects": ["业务控制"],
            "identifiers": ["CTL-9999"],
            "requested_facts": ["负责人"],
            "constraints": [],
            "retrieval_queries": ["业务控制 CTL-9999 负责人"],
        }
    )
    service = QueryUnderstandingService(settings(), router)  # type: ignore[arg-type]
    with session() as db:
        plan = service.understand(db, "那个业务控制最后归谁负责？")
    assert plan.identifiers == ()


def test_bare_key_uses_no_model_call() -> None:
    router = PlanningRouter({})
    service = QueryUnderstandingService(settings(), router)  # type: ignore[arg-type]
    with session() as db:
        plan = service.understand(db, "雪松门")
    assert plan.strategy == "deterministic-fallback-v1"
    assert plan.subjects == ("雪松门",)
    assert router.calls == []


def test_invalid_model_json_falls_back_safely() -> None:
    router = PlanningRouter("{not-json")
    service = QueryUnderstandingService(settings(), router)  # type: ignore[arg-type]
    with session() as db:
        plan = service.understand(db, "随口问下，雪松门背后是谁管的？")
    assert plan.strategy == "deterministic-fallback-v1"
    assert plan.fallback_reason == "JSONDecodeError"


def test_successful_semantic_plan_is_cached() -> None:
    router = PlanningRouter(
        {
            "language": "zh",
            "intent": "lookup",
            "subjects": ["雪松门"],
            "identifiers": [],
            "requested_facts": ["治理责任人"],
            "constraints": [],
            "retrieval_queries": ["雪松门 治理责任人"],
        }
    )
    service = QueryUnderstandingService(settings(), router)  # type: ignore[arg-type]
    with session() as db:
        first = service.understand(db, "随口问下，雪松门背后是谁管的？")
        second = service.understand(db, "随口问下，雪松门背后是谁管的？")
    assert first == second
    assert len(router.calls) == 1


def test_fallback_plan_preserves_exact_identifiers() -> None:
    assert fallback_query_plan("帮我找 CTL-4616 的材料", "disabled").identifiers == (
        "CTL-4616",
    )


def test_english_subject_anchor_removes_only_generic_context_words() -> None:
    plan = fallback_query_plan("Greenlake Credit", "test")
    plan = plan.__class__(
        strategy="semantic-qwen-v1",
        language="en",
        intent="lookup",
        subjects=("Greenlake credit operation",),
        identifiers=(),
        requested_facts=("approval role",),
        constraints=("APAC North team",),
        retrieval_queries=(),
    )
    assert plan.anchor_signals == (
        "Greenlake credit operation",
        "Greenlake credit",
    )
