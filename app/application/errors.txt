"""应用层可预期异常。

Controller 只需要把这些异常翻译成 HTTP 状态码；业务代码不直接依赖 FastAPI。
"""

from __future__ import annotations


class ApplicationError(Exception):
    """所有可安全暴露给调用方的应用异常基类。"""

    status_code = 500

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class InvalidRequestError(ApplicationError):
    """请求内容符合传输格式，但不满足业务约束。"""

    status_code = 422


class ResourceNotFoundError(ApplicationError):
    """请求的业务资源不存在或已不可见。"""

    status_code = 404


class ResourceConflictError(ApplicationError):
    """资源当前状态不允许执行目标操作。"""

    status_code = 409


class UnsupportedDocumentError(ApplicationError):
    """上传文件格式不在系统支持范围内。"""

    status_code = 415


class UploadTooLargeError(ApplicationError):
    """上传内容超过配置允许的最大体积。"""

    status_code = 413


class UpstreamServiceError(ApplicationError):
    """外部模型或其响应契约暂时不可用。"""

    status_code = 502

