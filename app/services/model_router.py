"""Deterministic Plus/Max routing, quota estimates, and model failover."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ModelUsage
from app.integrations.qwen import ChatCallResult, QwenAPIError, QwenClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """模型注册表中的不可变路由配置。"""

    id: str
    tier: str
    quota_tokens: int
    priority: int
    enabled: bool
    stable: bool


class NoModelAvailable(RuntimeError):
    """没有满足启用状态、层级和本地配额条件的模型。"""


class ModelRegistry:
    """加载并查询按层级、优先级排序的模型注册表。"""

    def __init__(self, models: list[RegisteredModel]):
        """保存稳定排序后的模型列表，确保降级顺序可预测。"""

        self.models = sorted(models, key=lambda item: (item.tier, item.priority))

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        """从 YAML 配置加载模型；空注册表视为部署配置错误。"""

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        models = [RegisteredModel(**row) for row in data.get("models", [])]
        if not models:
            raise ValueError(f"No models configured in {path}")
        return cls(models)

    def by_id(self, model_id: str) -> RegisteredModel | None:
        """按供应商模型 ID 查找注册项。"""

        return next((model for model in self.models if model.id == model_id), None)

    def by_tier(self, tier: str) -> list[RegisteredModel]:
        """返回指定层级中已启用的模型，顺序即调用优先级。"""

        return [model for model in self.models if model.tier == tier and model.enabled]


COMPLEX_QUERY_MARKERS = {
    "比较",
    "对比",
    "归纳",
    "综合",
    "差异",
    "为什么",
    "跨系统",
    "compare",
    "contrast",
    "summarize across",
    "difference",
    "differences",
}


def route_tier(question: str, evidence_document_ids: list[str], has_conflict: bool = False) -> str:
    """依据冲突、跨文档和复杂意图，确定回答所需的模型层级。"""

    normalized = question.casefold()
    if has_conflict or len(set(evidence_document_ids)) >= 2:
        return "max"
    if any(marker in normalized for marker in COMPLEX_QUERY_MARKERS):
        return "max"
    return "plus"


class QwenModelRouter:
    """执行本地配额保护、用量审计和同层模型故障转移。"""

    def __init__(self, registry: ModelRegistry, client: QwenClient):
        """注入模型注册表和实际的千问 API 客户端。"""

        self.registry = registry
        self.client = client

    def used_tokens(self, db: Session, model_id: str) -> int:
        """统计指定模型已成功消费的 Token；失败请求不占本地额度。"""

        statement = select(func.coalesce(func.sum(ModelUsage.total_tokens), 0)).where(
            ModelUsage.model_id == model_id,
            ModelUsage.result_status == "success",
        )
        return int(db.scalar(statement) or 0)

    def eligible(self, db: Session, tier: str) -> list[RegisteredModel]:
        """筛选仍低于硬阈值的候选模型，并在接近额度时告警。"""

        result = []
        for model in self.registry.by_tier(tier):
            used = self.used_tokens(db, model.id)
            # 80% 只告警，90% 才停止派发，为并发中的在途请求预留缓冲。
            if used >= int(model.quota_tokens * 0.8):
                logger.warning(
                    "Model %s has used %.1f%% of its configured local quota",
                    model.id,
                    100 * used / model.quota_tokens,
                )
            if used < int(model.quota_tokens * 0.9):
                result.append(model)
        return result

    def usage_rows(self, db: Session) -> list[dict]:
        """生成运维接口使用的各模型配额概览。"""

        rows = []
        for model in sorted(self.registry.models, key=lambda item: (item.tier, item.priority)):
            used = self.used_tokens(db, model.id)
            rows.append(
                {
                    "model_id": model.id,
                    "tier": model.tier,
                    "quota_tokens": model.quota_tokens,
                    "used_tokens": used,
                    "remaining_tokens": max(0, model.quota_tokens - used),
                    "usage_ratio": used / model.quota_tokens if model.quota_tokens else 1.0,
                    "enabled": model.enabled,
                }
            )
        return rows

    @staticmethod
    def _record(
        db: Session,
        *,
        model_id: str,
        request_id: str,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0,
        error_code: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        """持久化一次模型调用结果，作为配额统计和故障审计依据。"""

        db.add(
            ModelUsage(
                model_id=model_id,
                request_id=request_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                result_status=status,
                latency_ms=latency_ms,
                error_code=error_code,
                prompt_version=prompt_version,
            )
        )
        db.commit()

    def call(
        self,
        db: Session,
        *,
        tier: str,
        system_prompt: str,
        user_prompt: str,
        pinned_model: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatCallResult:
        """按优先级调用候选模型；固定模型请求失败时不跨模型降级。"""

        if pinned_model:
            # 固定模型用于可重复评测或显式调试，自动 fallback 会破坏可比性。
            configured = self.registry.by_id(pinned_model)
            if not configured or not configured.enabled:
                raise NoModelAvailable(f"Pinned model is not configured or enabled: {pinned_model}")
            candidates = [configured]
        else:
            candidates = self.eligible(db, tier)
        if not candidates:
            raise NoModelAvailable(
                f"No model with remaining local quota is available for tier {tier}"
            )

        last_error: QwenAPIError | None = None
        for model in candidates:
            local_request_id = str(uuid.uuid4())
            try:
                result = self.client.chat_json(
                    model_id=model.id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                )
                self._record(
                    db,
                    model_id=model.id,
                    request_id=result.request_id,
                    status="success",
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    total_tokens=result.usage.total_tokens,
                    latency_ms=result.latency_ms,
                    prompt_version=prompt_version,
                )
                return result
            except QwenAPIError as exc:
                last_error = exc
                self._record(
                    db,
                    model_id=model.id,
                    request_id=local_request_id,
                    status="failed",
                    error_code=exc.code,
                    prompt_version=prompt_version,
                )
                logger.warning(
                    "Qwen model %s failed with code %s; trying fallback", model.id, exc.code
                )
                if pinned_model:
                    break
        code = last_error.code if last_error else "unknown"
        raise NoModelAvailable(f"All candidate models failed (code={code})")
