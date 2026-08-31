"""Deterministic provider substitutes used by offline benchmark modes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.openai_client import ChatCallResult, ModelAPIError, Usage
from app.integrations.search import _lexical_signals

EVIDENCE_BLOCK = re.compile(
    r"\[EVIDENCE id=(?P<id>[^\]]+)]\n"
    r"file=(?P<file>[^\n]*)\nstatus=(?P<status>[^\n]*)\n"
    r"version=(?P<version>[^\n]*)\nlocation=(?P<location>[^\n]*)\n"
    r"(?P<content>.*?)\n\[/EVIDENCE]",
    re.DOTALL,
)


class OfflineBenchmarkQwen:
    """Make scale validation deterministic and guarantee zero external API calls."""

    def embeddings(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        del texts
        raise ModelAPIError("offline benchmark", code="offline_benchmark")

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        del query, documents, top_n
        raise ModelAPIError("offline benchmark", code="offline_benchmark")


def _offline_feature_vector(text: str, dimensions: int = 1024) -> list[float]:
    normalized = text.casefold()
    features = re.findall(r"[a-z0-9][a-z0-9_-]*", normalized)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    for run in cjk_runs:
        features.extend(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    vector = [0.0] * dimensions
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        vector[raw % dimensions] += 1.0 if (raw >> 10) & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class OfflineHybridQwen(OfflineBenchmarkQwen):
    """Deterministic feature hashing for mechanical k-NN/RRF/rerank tests."""

    def embeddings(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        return [_offline_feature_vector(text) for text in texts], Usage()

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        query_vector = _offline_feature_vector(query)
        signals = [signal.casefold() for signal in _lexical_signals(query)]
        scored = []
        for index, document in enumerate(documents):
            vector = _offline_feature_vector(document)
            score = sum(left * right for left, right in zip(query_vector, vector))
            normalized_document = document.casefold()
            score += 10.0 * sum(signal in normalized_document for signal in signals)
            scored.append((index, score))
        return sorted(scored, key=lambda row: row[1], reverse=True)[:top_n]


class EvidenceBoundBenchmarkRouter:
    """Emit deterministic answers from expected facts already present in evidence.

    This is intentionally not a model-quality test. It exercises the same
    AnswerService JSON parsing, citation-ID, exact-quote, persistence, routing,
    and strict refusal path at corpus scale without masking provider outages.
    """

    def __init__(self, expected_by_question: dict[str, dict[str, Any]]):
        self.expected_by_question = expected_by_question
        self.tier_calls: Counter[str] = Counter()

    @staticmethod
    def _line_quote(content: str, value: str) -> str:
        for line in content.splitlines():
            if value.casefold() in line.casefold():
                return line.strip()
        return value

    def call(
        self,
        db: Session,
        *,
        tier: str,
        system_prompt: str,
        user_prompt: str,
        pinned_model: str | None = None,
        prompt_version: str | None = None,
    ) -> ChatCallResult:
        del db, system_prompt, prompt_version
        question = user_prompt.split("QUESTION:\n", 1)[1].split("\n\nEVIDENCE:\n", 1)[0]
        expected = self.expected_by_question[question]
        blocks = [match.groupdict() for match in EVIDENCE_BLOCK.finditer(user_prompt)]
        expected_filenames = expected.get("expected_filenames", [expected["expected_filename"]])
        expected_blocks = [block for block in blocks if block["file"] in expected_filenames]
        if not expected_blocks or not set(expected_filenames).issubset(
            {block["file"] for block in expected_blocks}
        ):
            payload = {
                "status": "insufficient_evidence",
                "answer": "没有足够证据。",
                "claims": [],
            }
        else:
            claims = []
            for value in expected["answer_contains"]:
                value = str(value)
                block = next(
                    (
                        item
                        for item in expected_blocks
                        if value.casefold() in item["content"].casefold()
                    ),
                    None,
                )
                if block is None:
                    payload = {
                        "status": "insufficient_evidence",
                        "answer": "证据块没有包含预期事实。",
                        "claims": [],
                    }
                    break
                claims.append(
                    {
                        "text": value,
                        "evidence": [
                            {
                                "id": block["id"],
                                "quote": self._line_quote(block["content"], value),
                            }
                        ],
                    }
                )
            else:
                payload = {
                    "status": "answered",
                    "answer": "；".join(str(value) for value in expected["answer_contains"]),
                    "claims": claims,
                }
        self.tier_calls[tier] += 1
        return ChatCallResult(
            model_id=pinned_model or f"offline-{tier}",
            request_id=str(uuid.uuid4()),
            content=json.dumps(payload, ensure_ascii=False),
            usage=Usage(),
            latency_ms=0,
        )
