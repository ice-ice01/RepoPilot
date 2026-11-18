"""请求 ID 贯穿与结构化日志。

每个 HTTP 请求进入时生成短 request_id 存入 ContextVar：中间件、业务代码、后台任务
（创建时继承上下文）以及所有 logging 记录都会自动携带同一个 ID；AuditLog 也会落库。
LOG_FORMAT=json 时输出单行 JSON，便于采集；默认 text。
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str] = ContextVar("devflow_request_id", default="-")

_REQUEST_ID_HEADER = "x-request-id"


def current_request_id() -> str:
    return request_id_var.get()


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class RequestIdFilter(logging.Filter):
    """把 ContextVar 中的 request_id 注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None, log_format: str | None = None) -> None:
    """应用统一的日志配置；可在 .env 用 LOG_LEVEL / LOG_FORMAT 覆盖。"""
    from app.core.config import settings

    resolved_level = (level or settings.log_level or "INFO").upper()
    resolved_format = (log_format or settings.log_format or "text").lower()
    root = logging.getLogger()
    root.setLevel(resolved_level)
    # 避免重复 add handler（测试中多次调用）。
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if resolved_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] [req:%(request_id)s] %(message)s"))
    root.addHandler(handler)
    root.addFilter(RequestIdFilter())


def reset_request_id(incoming: str | None = None) -> str:
    """为当前上下文生成/继承请求 ID；只接受安全字符，防日志注入。"""
    candidate = "".join(ch for ch in (incoming or "").strip()[:32] if ch.isalnum() or ch in "-_")
    value = candidate or new_request_id()
    request_id_var.set(value)
    return value


class RequestIdMiddleware:
    """纯 ASGI 中间件：为每个请求生成 request_id 并写回 X-Request-ID 响应头。"""

    def __init__(self, app):  # noqa: ANN001 - ASGI callable
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = reset_request_id(incoming)

        async def send_with_request_id(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)
