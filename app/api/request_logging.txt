"""HTTP 请求访问日志与未处理异常日志。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app.core.logging import reset_request_id, set_request_id

logger = logging.getLogger(__name__)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _request_id(request: Request) -> str:
    """复用合法的上游请求 ID，否则生成新的 UUID。"""

    candidate = request.headers.get("X-Request-ID", "").strip()
    if candidate and _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


async def log_http_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """记录请求方法、路径、状态和耗时；异常时保留完整堆栈。"""

    request_id = _request_id(request)
    context_token = set_request_id(request_id)
    started_at = time.perf_counter()
    client = request.client.host if request.client else "unknown"
    logger.info(
        "HTTP请求开始 method=%s path=%s client=%s",
        request.method,
        request.url.path,
        client,
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "HTTP请求异常 method=%s path=%s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            (time.perf_counter() - started_at) * 1000,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        log = logger.warning if response.status_code >= 400 else logger.info
        log(
            "HTTP请求完成 method=%s path=%s status=%d elapsed_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        reset_request_id(context_token)
