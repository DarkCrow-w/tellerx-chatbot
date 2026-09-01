"""把应用层异常统一翻译为 HTTP 错误。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import HTTPException

from app.application.errors import ApplicationError

logger = logging.getLogger(__name__)


def run_application[T](action: Callable[[], T]) -> T:
    """执行应用用例，并保持所有 Controller 的错误响应格式一致。"""

    try:
        return action()
    except ApplicationError as exc:
        # 业务异常不打印堆栈；类型和 HTTP 状态足以排查，且避免重复记录用户输入。
        logger.warning(
            "应用请求被拒绝 error=%s status=%d",
            type(exc).__name__,
            exc.status_code,
        )
        raise HTTPException(exc.status_code, exc.detail) from exc
