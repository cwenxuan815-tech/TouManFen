"""统一错误码与错误响应构造（阶段 2/3）。

注意：错误信息不得包含密钥、请求体原文或服务端堆栈。
"""

from __future__ import annotations

from typing import Any, Mapping

ERROR_INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
ERROR_INTERNAL = "INTERNAL_ERROR"

# 阶段 3：分类服务错误码
ERROR_INVALID_TITLE = "INVALID_TITLE"
ERROR_TITLE_TOO_LONG = "TITLE_TOO_LONG"
ERROR_UNKNOWN_MODEL = "UNKNOWN_MODEL"
ERROR_MODEL_DISABLED = "MODEL_DISABLED"
ERROR_INVALID_MODELS = "INVALID_MODELS"
ERROR_WORKER_TIMEOUT = "WORKER_TIMEOUT"
ERROR_WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
ERROR_WORKER_HTTP_ERROR = "WORKER_HTTP_ERROR"
ERROR_WORKER_BAD_RESPONSE = "WORKER_BAD_RESPONSE"
ERROR_MODEL_ERROR = "MODEL_ERROR"
ERROR_INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"


class ConfigError(RuntimeError):
    """配置加载/校验失败。

    message 允许包含配置文件名与字段名，但不得包含敏感值。
    """

    def __init__(self, message: str, *, file: str | None = None, field: str | None = None):
        self.file = file
        self.field = field
        super().__init__(message)


class ApiError(Exception):
    """可直接映射为统一错误响应的业务异常。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details) if details else None
        super().__init__(message)


def error_payload(
    request_id: str,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "error": {"code": code, "message": message},
    }
    if details:
        body["error"]["details"] = dict(details)
    return body
