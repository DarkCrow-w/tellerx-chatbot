"""OpenAI-compatible SDK adapter for chat and embedding model gateways."""

from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import openai
import truststore

from app.core.config import Settings

logger = logging.getLogger(__name__)


class ModelAPIError(RuntimeError):
    """保留 HTTP 状态和供应商错误码的统一模型网关异常。"""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        """记录安全截断后的错误消息及可用于审计的元数据。"""

        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class Usage:
    """一次模型调用返回的 Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class ChatCallResult:
    """聊天接口的标准化结果，屏蔽内部网关响应细节。"""

    model_id: str
    request_id: str
    content: str
    usage: Usage
    latency_ms: float


class OpenAIModelClient:
    """通过公司兼容端点调用 Chat 和 Embedding，并复用 HTTP/2 连接池。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        """使用系统证书信任库创建 OpenAI SDK 客户端。"""

        self.settings = settings
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._http = httpx.Client(
            http2=True,
            verify=ssl_context,
            timeout=self.settings.model_api_timeout_seconds,
            transport=transport,
        )
        self._client = openai.OpenAI(
            api_key=self.settings.require_model_api_key(),
            base_url=self.settings.model_api_base_url.rstrip("/") + "/",
            http_client=self._http,
            timeout=self.settings.model_api_timeout_seconds,
            max_retries=self.settings.model_api_max_retries,
        )

    def close(self) -> None:
        """关闭 SDK 及其持有的 HTTP 连接池。"""

        self._client.close()

    @staticmethod
    def _usage(raw: object | None) -> Usage:
        """兼容 Chat 和 Embedding 的 SDK 用量对象。"""

        prompt = int(
            getattr(raw, "prompt_tokens", 0)
            or getattr(raw, "input_tokens", 0)
            or 0
        )
        completion = int(
            getattr(raw, "completion_tokens", 0)
            or getattr(raw, "output_tokens", 0)
            or 0
        )
        total = int(getattr(raw, "total_tokens", 0) or prompt + completion)
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

    @staticmethod
    def _translate_error(exc: openai.OpenAIError) -> ModelAPIError:
        """把 SDK 异常转换为业务层稳定依赖的错误类型。"""

        status_code = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        code: str | None = None
        if isinstance(body, dict):
            nested = body.get("error")
            code = str(
                body.get("code")
                or (nested.get("code") if isinstance(nested, dict) else "")
                or ""
            ) or None
        if code is None:
            code = str(getattr(exc, "code", "") or "") or type(exc).__name__
        return ModelAPIError(
            str(exc)[:500],
            status_code=int(status_code) if status_code is not None else None,
            code=code,
        )

    def embeddings(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        """按输入顺序返回文本向量，并校验数量和维度。"""

        if not texts:
            return [], Usage()
        started = time.perf_counter()
        logger.info(
            "Embedding调用开始 model=%s batch_size=%d dimensions=%d",
            self.settings.embedding_model,
            len(texts),
            self.settings.embedding_dimensions,
        )
        try:
            response = self._client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts,
                dimensions=self.settings.embedding_dimensions,
                encoding_format="float",
            )
        except openai.OpenAIError as exc:
            translated = self._translate_error(exc)
            logger.warning(
                "Embedding调用失败 model=%s status=%s code=%s elapsed_ms=%.1f",
                self.settings.embedding_model,
                translated.status_code,
                translated.code,
                (time.perf_counter() - started) * 1000,
            )
            raise translated from exc
        rows = sorted(response.data, key=lambda row: row.index)
        embeddings = [list(row.embedding) for row in rows]
        if len(embeddings) != len(texts):
            raise ModelAPIError(
                "Embedding response count does not match input",
                code="invalid_response",
            )
        invalid_dimension = next(
            (
                len(vector)
                for vector in embeddings
                if len(vector) != self.settings.embedding_dimensions
            ),
            None,
        )
        if invalid_dimension is not None:
            logger.error(
                "Embedding响应维度错误 model=%s expected=%d actual=%d",
                self.settings.embedding_model,
                self.settings.embedding_dimensions,
                invalid_dimension,
            )
            raise ModelAPIError(
                "Embedding dimension mismatch: expected "
                f"{self.settings.embedding_dimensions}, got {invalid_dimension}",
                code="invalid_response",
            )
        usage = self._usage(response.usage)
        logger.info(
            "Embedding调用完成 model=%s batch_size=%d tokens=%d elapsed_ms=%.1f",
            self.settings.embedding_model,
            len(texts),
            usage.total_tokens,
            (time.perf_counter() - started) * 1000,
        )
        return embeddings, usage

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """明确拒绝 Rerank 调用，保证该版本不会访问不存在的内部接口。"""

        del query, documents, top_n
        raise ModelAPIError("Rerank is disabled", code="rerank_disabled")

    def chat_json(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1600,
    ) -> ChatCallResult:
        """以零温度调用 Chat Completion，并返回待业务校验的 JSON 文本。"""

        request: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if self.settings.model_api_json_mode_enabled:
            request["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        logger.info(
            "Chat调用开始 model=%s max_tokens=%d json_mode=%s",
            model_id,
            max_tokens,
            self.settings.model_api_json_mode_enabled,
        )
        try:
            response = self._client.chat.completions.create(**request)
        except openai.OpenAIError as exc:
            translated = self._translate_error(exc)
            logger.warning(
                "Chat调用失败 model=%s status=%s code=%s elapsed_ms=%.1f",
                model_id,
                translated.status_code,
                translated.code,
                (time.perf_counter() - started) * 1000,
            )
            raise translated from exc
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelAPIError(
                "Chat response has an invalid shape",
                code="invalid_response",
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelAPIError(
                "Chat response did not contain text content",
                code="invalid_response",
            )
        result = ChatCallResult(
            model_id=str(response.model or model_id),
            request_id=str(response.id or uuid.uuid4()),
            content=content,
            usage=self._usage(response.usage),
            latency_ms=latency_ms,
        )
        logger.info(
            "Chat调用完成 model=%s provider_request_id=%s tokens=%d elapsed_ms=%.1f",
            result.model_id,
            result.request_id,
            result.usage.total_tokens,
            result.latency_ms,
        )
        return result


def parse_json_object(content: str) -> dict[str, Any]:
    """解析模型 JSON 对象，并兼容偶发的 Markdown 代码围栏。"""

    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise TypeError("Expected a JSON object")
    return parsed
