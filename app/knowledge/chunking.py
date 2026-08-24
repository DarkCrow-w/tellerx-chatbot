"""Deterministic text and table chunking with stable provenance metadata."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedUnit:
    text: str
    heading_path: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    is_table: bool = False


@dataclass(slots=True)
class TextChunk:
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    heading_path: str | None
    page_number: int | None
    sheet_name: str | None
    cell_range: str | None


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[A-Za-z0-9_./:+#-]+")


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    words = len(_WORD.findall(text))
    punctuation = max(0, len(text) - cjk - sum(len(x) for x in _WORD.findall(text)))
    return max(1, cjk + int(words * 1.3) + punctuation // 4)


def _split_long_text(text: str, max_tokens: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return []
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for paragraph in paragraphs:
        paragraph_tokens = estimate_tokens(paragraph)
        if paragraph_tokens > max_tokens:
            sentences = [s.strip() for s in re.split(r"(?<=[。！？.!?；;])\s*", paragraph) if s.strip()]
        else:
            sentences = [paragraph]
        for sentence in sentences:
            tokens = estimate_tokens(sentence)
            if current and current_tokens + tokens > max_tokens:
                pieces.append("\n\n".join(current))
                current, current_tokens = [], 0
            if tokens > max_tokens:
                chars_per_token = max(1, len(sentence) // tokens)
                char_limit = max(200, max_tokens * chars_per_token)
                for start in range(0, len(sentence), char_limit):
                    segment = sentence[start : start + char_limit].strip()
                    if segment:
                        pieces.append(segment)
                continue
            current.append(sentence)
            current_tokens += tokens
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def chunk_units(
    units: list[ParsedUnit],
    *,
    target_tokens: int = 450,
    max_tokens: int = 650,
    overlap_tokens: int = 60,
) -> list[TextChunk]:
    del target_tokens  # max size is the hard invariant; source structure controls natural boundaries.
    chunks: list[TextChunk] = []
    ordinal = 0
    for unit in units:
        parts = _split_long_text(unit.text, max_tokens)
        previous_tail = ""
        for index, part in enumerate(parts):
            content = part
            if index > 0 and not unit.is_table and previous_tail:
                content = f"{previous_tail}\n\n{part}"
                if estimate_tokens(content) > max_tokens:
                    content = part
            normalized = content.strip()
            if not normalized:
                continue
            token_count = estimate_tokens(normalized)
            chunks.append(
                TextChunk(
                    ordinal=ordinal,
                    content=normalized,
                    content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    token_count=token_count,
                    heading_path=unit.heading_path,
                    page_number=unit.page_number,
                    sheet_name=unit.sheet_name,
                    cell_range=unit.cell_range,
                )
            )
            ordinal += 1
            if not unit.is_table:
                words = re.findall(r"\S+", part)
                previous_tail = " ".join(words[-overlap_tokens:])
    return chunks
