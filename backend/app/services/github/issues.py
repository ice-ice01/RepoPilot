from datetime import datetime, timezone

from app.services.github.client import GitHubClient


async def list_issues(
    owner: str,
    repo: str,
    token: str | None = None,
    limit: int = 30,
    base_url: str | None = None,
    since: datetime | None = None,
) -> list[dict]:
    client = GitHubClient(token, base_url)
    # 当前只拉第一页，limit 是 per_page，不会自动遍历全部历史；GitHub 把 PR 也放在 /issues 中。
    params: dict[str, object] = {"state": "all", "per_page": limit}
    if since is not None:
        # GitHub 的 since 只返回 updated_at 在该时刻之后的 Issue，供增量同步使用。
        aware_since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        params["since"] = aware_since.isoformat()
    items = await client.get(f"/repos/{owner}/{repo}/issues", params)
    return [item for item in items if "pull_request" not in item]
