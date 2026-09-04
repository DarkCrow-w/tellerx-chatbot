"""Deterministic document-name normalization and bounded fragment matching."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

GENERIC_DOCUMENT_TERMS = {
    "文档",
    "文件",
    "方案",
    "设计",
    "说明",
    "说明书",
    "架构",
    "接口",
    "系统",
    "document",
    "file",
    "design",
    "architecture",
    "spec",
    "specification",
}
_SEPARATORS = re.compile(r"[\s_\-—–·()（）\[\]【】{}]+")
_TOKENS = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*|[\u3400-\u9fff]+")
_KNOWN_EXTENSION = re.compile(
    r"\.(?:docx?|xlsx?|xlsm|pdf|html?|md|markdown|txt|csv)$", re.IGNORECASE
)


def normalize_document_name(value: str) -> str:
    """Normalize a filename or user-supplied filename fragment without guessing."""

    name = PurePath(str(value).replace("\\", "/")).name
    name = _KNOWN_EXTENSION.sub("", name)
    name = unicodedata.normalize("NFKC", name).casefold()
    name = _SEPARATORS.sub(" ", name)
    name = re.sub(r"[^a-z0-9.\u3400-\u9fff ]+", " ", name)
    return " ".join(name.split())


def compact_document_name(value: str) -> str:
    return normalize_document_name(value).replace(" ", "")


def document_name_tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKENS.findall(normalize_document_name(value)))


def meaningful_document_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in document_name_tokens(value)
        if token not in GENERIC_DOCUMENT_TERMS and len(token) >= 2
    )


def has_meaningful_document_hint(value: str) -> bool:
    normalized = normalize_document_name(value)
    if not normalized:
        return False
    if normalized in GENERIC_DOCUMENT_TERMS:
        return False
    meaningful = meaningful_document_tokens(normalized)
    if meaningful:
        return True
    compact = normalized.replace(" ", "")
    return len(compact) >= 2 and compact not in GENERIC_DOCUMENT_TERMS


def score_document_name(hint: str, filename: str) -> float:
    """Score only explainable exact, substring, and complete-token matches.

    The function intentionally does not use edit distance or semantic similarity.
    A score of zero means the document must not be treated as a filename match.
    """

    normalized_hint = normalize_document_name(hint)
    normalized_name = normalize_document_name(filename)
    if not has_meaningful_document_hint(normalized_hint) or not normalized_name:
        return 0.0
    if normalized_hint == normalized_name:
        return 1.0

    compact_hint = normalized_hint.replace(" ", "")
    compact_name = normalized_name.replace(" ", "")
    if compact_hint and compact_hint in compact_name:
        coverage = min(1.0, len(compact_hint) / max(1, len(compact_name)))
        return min(0.99, 0.72 + coverage * 0.25)

    tokens = meaningful_document_tokens(normalized_hint)
    if tokens and all(token in normalized_name for token in tokens):
        covered = sum(len(token) for token in tokens)
        coverage = min(1.0, covered / max(1, len(compact_name)))
        return min(0.94, 0.62 + coverage * 0.25)
    return 0.0
