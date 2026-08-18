from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.model_router import ModelRegistry, NoModelAvailable, QwenModelRouter, route_tier
from app.models import ModelUsage
from app.qwen import QwenAPIError


def test_deterministic_tier_routing() -> None:
    assert route_tier("订单状态是什么？", ["doc-1"] * 6) == "plus"
    assert route_tier("请比较两个系统的差异", ["doc-1"] * 6) == "max"
    assert route_tier("总结规则", ["doc-1", "doc-2"]) == "max"
    assert route_tier("直接问题", ["doc-1"], has_conflict=True) == "max"


def test_registry_priority_and_quota(tmp_path: Path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  - id: plus-a
    tier: plus
    quota_tokens: 100
    priority: 10
    enabled: true
    stable: true
  - id: plus-b
    tier: plus
    quota_tokens: 100
    priority: 20
    enabled: true
    stable: false
""",
        encoding="utf-8",
    )
    registry = ModelRegistry.load(config)
    assert [model.id for model in registry.by_tier("plus")] == ["plus-a", "plus-b"]


def test_usage_table_can_track_model_tokens() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            ModelUsage(
                model_id="plus-a",
                request_id="request-1",
                prompt_tokens=40,
                completion_tokens=20,
                total_tokens=60,
                result_status="success",
                latency_ms=100,
                prompt_version="grounded-qa-v1",
            )
        )
        db.commit()
        assert db.query(ModelUsage).one().total_tokens == 60
        assert db.query(ModelUsage).one().prompt_version == "grounded-qa-v1"


def test_router_does_not_expose_provider_error_message(tmp_path: Path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  - id: plus-a
    tier: plus
    quota_tokens: 100
    priority: 10
    enabled: true
    stable: true
""",
        encoding="utf-8",
    )

    class FailingClient:
        def chat_json(self, **_: object) -> None:
            raise QwenAPIError(
                "provider body may contain sensitive diagnostics",
                status_code=400,
                code="invalid_request",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    router = QwenModelRouter(ModelRegistry.load(config), FailingClient())  # type: ignore[arg-type]
    with Session(engine) as db:
        try:
            router.call(
                db,
                tier="plus",
                system_prompt="system",
                user_prompt="question",
            )
        except NoModelAvailable as exc:
            assert "invalid_request" in str(exc)
            assert "sensitive diagnostics" not in str(exc)
        else:
            raise AssertionError("Expected NoModelAvailable")
