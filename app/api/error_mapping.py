"""把应用层异常统一翻译为 HTTP 错误。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException

from app.application.errors import ApplicationError


def run_application[T](action: Callable[[], T]) -> T:
    """执行应用用例，并保持所有 Controller 的错误响应格式一致。"""

    try:
        return action()
    except ApplicationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
