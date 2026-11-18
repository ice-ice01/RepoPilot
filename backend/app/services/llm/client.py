import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.services.context_compression import prompt_cache_model_kwargs

logger = logging.getLogger(__name__)

# 可安全重试的临时性错误：请求超时与常见网关/限流状态码。
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 8.0

# 模块级共享连接池：所有 LLM 请求复用 TCP/TLS 连接；超时按单次请求覆盖。
_shared_client: httpx.AsyncClient | None = None


def _shared_http_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=60)
    return _shared_client


async def close_shared_http_client() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


def _stream_content(chunk: Any) -> str | None:
    """Extract streamed text while tolerating provider metadata-only chunks."""
    if not isinstance(chunk, dict):
        return None
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return str(content) if content else None


def _retry_delay_seconds(attempt: int, response: httpx.Response | None) -> float:
    delay = _BASE_BACKOFF_SECONDS * (2 ** attempt)
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                pass
    return delay


class LLMJSONError(RuntimeError):
    """Safe diagnostic code, without provider bodies, URLs or credentials."""


class LLMClient:
    def __init__(self) -> None:
        # Planner 和专用 Agent 共用项目模型配置，不在 Planner 内写死模型名称。
        # settings 优先读取 AI_MODEL / AI_BASE_URL / AI_API_KEY，也兼容 LLM_* 配置。
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model

    async def chat_json(
        self, system_prompt: str, user_payload: dict[str, Any], *,
        timeout_seconds: float = 60, raise_on_error: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """向配置的模型服务发送一次请求，取得 JSON 对象；本方法不保存对话历史。"""
        # 这是专用 Agent 使用的薄客户端：直接调用 OpenAI-compatible HTTP 接口，
        # 只要求模型返回 JSON 对象。response_format 只约束传输层形态；真正的业务
        # 字段、枚举和范围约束由各 Agent 调用的 Pydantic Output model 完成。
        if not self.api_key:
            if raise_on_error:
                raise LLMJSONError("not_configured")
            return {}

        # 每次请求只有两条消息：system 是规划规则/能力说明，user 是本次结构化上下文。
        # 因而模型不会自动知道网页中的历史聊天，调用方必须明确把所需信息放进输入。
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        payload.update(prompt_cache_model_kwargs())
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        client = _shared_http_client()
        last_code: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.post(
                    # 真正发生网络请求的位置，使用 OpenAI-compatible 的对话接口。
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                # json.loads 把模型返回的 JSON 文本转成 Python 字典，方便后续业务校验。
                output = json.loads(content)
                if not isinstance(output, dict):
                    raise ValueError("Expected a JSON object")
                return output
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                retriable, response = self._classify_exception(exc)
                if retriable and attempt < MAX_ATTEMPTS - 1:
                    last_code = self._error_code(exc)
                    logger.warning(
                        "LLM JSON 请求失败（第 %s/%s 次尝试，code=%s），将退避重试",
                        attempt + 1, MAX_ATTEMPTS, last_code,
                    )
                    await asyncio.sleep(_retry_delay_seconds(attempt, response))
                    continue
                if raise_on_error:
                    raise LLMJSONError(self._error_code(exc)) from None
                # 保留原有契约：未配置 raise_on_error 时返回空字典，但留下告警日志，
                # 不再让网络抖动静默变成"空分析结果"。
                logger.warning(
                    "LLM JSON 请求最终失败（code=%s，已尝试 %s 次）", self._error_code(exc), attempt + 1
                )
                return {}
        return {}

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        if not self.api_key:
            return ""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        payload.update(prompt_cache_model_kwargs())
        client = _shared_http_client()
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=60,
                )
                response.raise_for_status()
                return str(response.json()["choices"][0]["message"].get("content") or "")
            except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                    retriable, _response = self._classify_exception(exc)
                    if retriable and attempt < MAX_ATTEMPTS - 1:
                        logger.warning(
                            "LLM 文本请求失败（第 %s/%s 次尝试，code=%s），将退避重试",
                            attempt + 1, MAX_ATTEMPTS, self._error_code(exc),
                        )
                        await asyncio.sleep(_retry_delay_seconds(attempt, None))
                        continue
                    logger.warning("LLM 文本请求最终失败（code=%s，已尝试 %s 次）", self._error_code(exc), attempt + 1)
                    return ""
        return ""

    async def chat_text_stream(self, system_prompt: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        if not self.api_key:
            return

        payload_messages = [{"role": "system", "content": system_prompt}, *messages]
        payload: dict[str, Any] = {"model": self.model, "messages": payload_messages, "stream": True}
        payload.update(prompt_cache_model_kwargs())
        client = _shared_http_client()
        # 只在建立连接与首个响应状态上重试；已经开始输出的流不能重放。
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=60,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        retriable = exc.response.status_code in RETRYABLE_STATUS_CODES
                        if retriable and attempt < MAX_ATTEMPTS - 1:
                            logger.warning(
                                "LLM 流式请求失败（第 %s/%s 次尝试，http_%s），将退避重试",
                                attempt + 1, MAX_ATTEMPTS, exc.response.status_code,
                            )
                            await asyncio.sleep(_retry_delay_seconds(attempt, exc.response))
                            continue
                        logger.warning("LLM 流式请求最终失败（http_%s）", exc.response.status_code)
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line.removeprefix("data: ").strip()
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        content = _stream_content(chunk)
                        if content:
                            yield content
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < MAX_ATTEMPTS - 1:
                    logger.warning(
                        "LLM 流式连接失败（第 %s/%s 次尝试，code=%s），将退避重试",
                        attempt + 1, MAX_ATTEMPTS, self._error_code(exc),
                    )
                    await asyncio.sleep(_retry_delay_seconds(attempt, None))
                    continue
                logger.warning("LLM 流式连接最终失败（code=%s）", self._error_code(exc))
                return

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[bool, httpx.Response | None]:
        """返回 (是否可重试, 关联响应)。只把临时性网络/限流/网关错误视为可重试。"""
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in RETRYABLE_STATUS_CODES, exc.response
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True, None
        # JSON 解析/结构错误是模型输出问题，重试同样的请求大概率同样失败。
        return False, None

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return f"http_{exc.response.status_code}"
        if isinstance(exc, httpx.RequestError):
            return "connection_error"
        return "invalid_response"
