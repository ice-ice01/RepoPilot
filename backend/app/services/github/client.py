from contextvars import ContextVar
from typing import Any

import httpx

from app.core.config import settings

# 模块级共享连接池：复用 TCP/TLS 连接，避免每个 API 调用重新握手。
# 每个请求仍按调用方传入的 token/base_url 鉴权与寻址。
_shared_client: httpx.AsyncClient | None = None

# 最近一次 GitHub 响应的限流信息（按请求上下文隔离），供同步健康面板记录。
rate_limit_info_var: ContextVar[dict[str, Any]] = ContextVar("devflow_github_rate_limit", default={})


def shared_http_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(follow_redirects=True, timeout=30)
    return _shared_client


async def close_shared_http_client() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


def _capture_rate_limit(response: httpx.Response) -> None:
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is None:
        return
    rate_limit_info_var.set({
        "remaining": remaining,
        "limit": response.headers.get("x-ratelimit-limit"),
        "reset": response.headers.get("x-ratelimit-reset"),
        "used": response.headers.get("x-ratelimit-used"),
    })


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        # Never lend a server-wide credential to another authenticated account.
        # Request callers supply the token encrypted on their own Repository.
        self.token = token
        self.base_url = (base_url or settings.github_api_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # 这是所有 GitHub GET 请求的公共出口，类似 Spring 中封装好的 WebClient/RestClient。
        # 上层 Service 只传 API path 和 query 参数，不需要重复处理鉴权与响应状态。
        response = await shared_http_client().get(
            f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=30
        )
        _capture_rate_limit(response)
        response.raise_for_status()
        return response.json()

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        response = await shared_http_client().get(
            f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=60
        )
        _capture_rate_limit(response)
        response.raise_for_status()
        return response.content

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await shared_http_client().post(
            f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=30
        )
        _capture_rate_limit(response)
        response.raise_for_status()
        return response.json()

    async def patch(self, path: str, payload: dict[str, Any]) -> Any:
        response = await shared_http_client().patch(
            f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=30
        )
        _capture_rate_limit(response)
        response.raise_for_status()
        return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return await self.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/labels", {"labels": labels})
