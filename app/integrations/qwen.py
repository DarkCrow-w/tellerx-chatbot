"""Minimal DashScope-compatible adapters for chat, embedding, and reranking."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class QwenAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class ChatCallResult:
    model_id: str
    request_id: str
    content: str
    usage: Usage
    latency_ms: float


class QwenClient:
    """Thread-safe Qwen HTTP adapter with process-local connection pooling."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        # A long-lived client reuses TLS handshakes and provider connections.
        # The application composition root closes it during graceful shutdown.
        self._http = httpx.Client(
            timeout=self.settings.qwen_timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self.settings.qwen_api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, url: str, payload: dict[str, Any]) -> tuple[dict, float]:
        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in range(self.settings.qwen_max_retries + 1):
            try:
                response = self._http.request(method, url, json=payload)
                if response.status_code < 400:
                    return response.json(), (time.perf_counter() - started) * 1000
                body: dict[str, Any] = {}
                try:
                    body = response.json()
                except ValueError:
                    pass
                code = str(body.get("code") or body.get("error", {}).get("code") or "http_error")
                message = str(
                    body.get("message")
                    or body.get("error", {}).get("message")
                    or f"Qwen API returned HTTP {response.status_code}"
                )[:500]
                error = QwenAPIError(message, status_code=response.status_code, code=code)
                if response.status_code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise error
                last_error = error
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            if attempt < self.settings.qwen_max_retries:
                time.sleep(0.5 * (2**attempt))
        if isinstance(last_error, QwenAPIError):
            raise last_error
        raise QwenAPIError(
            f"Qwen API transport failed: {type(last_error).__name__ if last_error else 'unknown'}",
            code="transport_error",
        )

    @staticmethod
    def _usage(data: dict) -> Usage:
        raw = data.get("usage") or {}
        prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        total = int(raw.get("total_tokens") or prompt + completion)
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

    def embeddings(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()
        payload = {
            "model": self.settings.qwen_embedding_model,
            "input": texts,
            "dimensions": self.settings.qwen_embedding_dimensions,
            "encoding_format": "float",
        }
        data, _ = self._request(
            "POST",
            f"{self.settings.qwen_chat_base_url.rstrip('/')}/embeddings",
            payload,
        )
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        embeddings = [row["embedding"] for row in rows]
        if len(embeddings) != len(texts):
            raise QwenAPIError("Embedding response count does not match input", code="invalid_response")
        return embeddings, self._usage(data)

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        payload = {
            "model": self.settings.qwen_rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": (
                "Given a business knowledge-base query, retrieve passages that directly support the answer."
            ),
        }
        data, _ = self._request(
            "POST",
            f"{self.settings.qwen_rerank_base_url.rstrip('/')}/reranks",
            payload,
        )
        results = data.get("results") or data.get("output", {}).get("results") or []
        ranked: list[tuple[int, float]] = []
        for row in results:
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if index is not None and score is not None:
                ranked.append((int(index), float(score)))
        if not ranked:
            raise QwenAPIError("Rerank response did not contain results", code="invalid_response")
        return ranked

    def chat_json(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1600,
    ) -> ChatCallResult:
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        data, latency_ms = self._request(
            "POST",
            f"{self.settings.qwen_chat_base_url.rstrip('/')}/chat/completions",
            payload,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise QwenAPIError("Chat response has an invalid shape", code="invalid_response") from exc
        return ChatCallResult(
            model_id=str(data.get("model") or model_id),
            request_id=str(data.get("id") or uuid.uuid4()),
            content=content,
            usage=self._usage(data),
            latency_ms=latency_ms,
        )


def parse_json_object(content: str) -> dict[str, Any]:
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
