from app.services.github.client import GitHubClient


async def list_pull_requests(owner: str, repo: str, token: str | None = None, limit: int = 30, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get(f"/repos/{owner}/{repo}/pulls", {"state": "all", "per_page": limit})


async def list_pull_request_files(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    # number 是仓库内的 PR 编号（例如 #42），不是本地 UUID；files/comments 当前也没有翻页循环。
    client = GitHubClient(token, base_url)
    return await client.get(f"/repos/{owner}/{repo}/pulls/{number}/files", {"per_page": 100})


async def list_pull_request_review_comments(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get(f"/repos/{owner}/{repo}/pulls/{number}/comments", {"per_page": 100})
