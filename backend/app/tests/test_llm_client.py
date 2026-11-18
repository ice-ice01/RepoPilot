import json

import httpx
import pytest

from app.core.config import settings
from app.services.llm.client import LLMClient, LLMJSONError, _stream_content


def test_stream_content_skips_empty_choices() -> None:
    assert _stream_content({"choices": []}) is None


def test_stream_content_extracts_delta_text() -> None:
    assert _stream_content({"choices": [{"delta": {"content": "chunk"}}]}) == "chunk"


@pytest.mark.asyncio
async def test_chat_json_success_uses_json_format_and_token_limit(monkeypatch) -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"tasks": []}'}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(settings, "llm_api_key", "unit-test-key")

    output = await LLMClient().chat_json("system", {"goal": "test"}, max_tokens=123)

    assert output == {"tasks": []}
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["max_tokens"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['[1, 2]', 'null', 'not-json'])
async def test_chat_json_rejects_non_object_or_invalid_json_without_leaking(monkeypatch, content) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(settings, "llm_api_key", "unit-test-key")

    assert await LLMClient().chat_json("system", {}, raise_on_error=False) == {}
    with pytest.raises(LLMJSONError, match="^invalid_response$"):
        await LLMClient().chat_json("system", {}, raise_on_error=True)


@pytest.mark.asyncio
async def test_chat_json_http_error_has_safe_status_code(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "provider secret"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(settings, "llm_api_key", "unit-test-key")

    with pytest.raises(LLMJSONError, match="^http_429$") as caught:
        await LLMClient().chat_json("system", {}, raise_on_error=True)
    assert "provider secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_chat_json_timeout_has_safe_code(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(settings, "llm_api_key", "unit-test-key")

    with pytest.raises(LLMJSONError, match="^timeout$"):
        await LLMClient().chat_json("system", {}, raise_on_error=True)


@pytest.mark.asyncio
async def test_chat_json_missing_key_is_safe(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", None)
    client = LLMClient()

    assert await client.chat_json("system", {}) == {}
    with pytest.raises(LLMJSONError, match="^not_configured$"):
        await client.chat_json("system", {}, raise_on_error=True)
