from __future__ import annotations

import asyncio
import io
import logging
import unittest

from fastapi import Request, Response

from app.api.request_logging import log_http_request
from app.core.logging import configure_logging, reset_request_id, set_request_id


def _request(path: str, request_id: str) -> Request:
    """构造不依赖数据库的最小 HTTP 请求。"""

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"x-request-id", request_id.encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
    )


class LoggingTest(unittest.TestCase):
    """验证统一格式、请求关联和异常堆栈不会在重构中丢失。"""

    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.previous_handlers = list(self.root.handlers)
        self.previous_level = self.root.level
        self.output = io.StringIO()
        configure_logging("INFO", stream=self.output)

    def tearDown(self) -> None:
        self.root.handlers.clear()
        self.root.handlers.extend(self.previous_handlers)
        self.root.setLevel(self.previous_level)

    def test_context_filter_adds_request_id(self) -> None:
        token = set_request_id("req-log-123")
        try:
            logging.getLogger("test.context").info("关键阶段完成")
        finally:
            reset_request_id(token)

        value = self.output.getvalue()
        self.assertIn("request_id=req-log-123", value)
        self.assertIn("关键阶段完成", value)

    def test_http_middleware_logs_success_and_returns_request_id(self) -> None:
        async def call_next(_: Request) -> Response:
            return Response(status_code=204)

        response = asyncio.run(
            log_http_request(_request("/health/live", "req-http-123"), call_next)
        )

        self.assertEqual(response.headers["X-Request-ID"], "req-http-123")
        value = self.output.getvalue()
        self.assertIn("HTTP请求开始", value)
        self.assertIn("status=204", value)
        self.assertIn("request_id=req-http-123", value)

    def test_http_middleware_logs_exception_traceback(self) -> None:
        async def call_next(_: Request) -> Response:
            raise RuntimeError("simulated failure")

        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            asyncio.run(log_http_request(_request("/boom", "req-error-123"), call_next))

        value = self.output.getvalue()
        self.assertIn("HTTP请求异常", value)
        self.assertIn("request_id=req-error-123", value)
        self.assertIn("Traceback (most recent call last)", value)
        self.assertIn("RuntimeError: simulated failure", value)


if __name__ == "__main__":
    unittest.main()
