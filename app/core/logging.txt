"""应用统一日志配置与请求上下文。"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar, Token
from typing import TextIO

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
)
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class RequestContextFilter(logging.Filter):
    """把当前请求 ID 注入每条日志，非 HTTP 场景使用 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def set_request_id(request_id: str) -> Token[str]:
    """设置当前异步上下文的请求 ID，并返回供调用方恢复的令牌。"""

    return _request_id.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """请求结束后恢复之前的上下文，防止 ID 串到下一次请求。"""

    _request_id.reset(token)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """配置控制台日志，并让第三方组件统一经过根 Logger 输出。

    应用日志只写标准输出，便于本地终端、IKP 和其他容器平台直接采集；
    不在应用进程内管理日志文件、轮转或保留周期。
    """

    normalized_level = level.strip().upper()
    try:
        numeric_level = _LOG_LEVELS[normalized_level]
    except KeyError as exc:
        raise ValueError(f"Unsupported LOG_LEVEL: {level}") from exc

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # 避免 HTTP SDK 在 INFO 级别重复打印请求；模型适配器会输出不含正文和 Token 的摘要。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    # Uvicorn 可能先安装自己的 Handler；统一交给根 Logger，保持请求 ID 和格式一致。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        component = logging.getLogger(name)
        component.handlers.clear()
        component.propagate = True
    # 应用中间件已经输出带 request_id 和耗时的访问日志，关闭无上下文的重复记录。
    logging.getLogger("uvicorn.access").disabled = True
