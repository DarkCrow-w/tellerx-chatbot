"""Provider-neutral evidence values exchanged by retrieval and answering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Evidence:
    """One traceable source fragment eligible to support an answer claim."""

    chunk_id: str
    document_id: str
    version_id: str
    project_id: str
    filename: str
    document_status: str
    document_type: str
    content: str
    heading_path: str | None = None
    section_id: str | None = None
    section_level: int | None = None
    breadcrumb: tuple[str, ...] = ()
    location_confidence: float | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    version_label: str | None = None
    score: float = 0.0


def source_status(source: dict[str, Any]) -> str:
    """兼容搜索投影与业务对象的状态字段名。"""

    return str(source.get("lifecycle_status") or source.get("document_status") or "")
