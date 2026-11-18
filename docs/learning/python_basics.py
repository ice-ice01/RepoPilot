"""Python 入门练习：纯标准库，不访问网络、数据库或模型。"""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


SAMPLE_ISSUES = [
    {
        "number": 101,
        "title": "Login API returns 500",
        "body": None,
        "state": "open",
        "labels": ["bug", "backend"],
    },
    {
        "number": 102,
        "title": "新增暗色模式",
        "body": "支持手动切换主题",
        "state": "open",
        "labels": ["feature", "frontend"],
    },
    {
        "number": 103,
        "title": "修正文档拼写",
        "body": "更新 README",
        "state": "closed",
        "labels": ["docs"],
    },
]


def classify_issue(title: str, labels: list[str] | None = None) -> str:
    """教学版分类器，不等同于生产 IssueAgent 的完整规则。"""
    text = " ".join([title, *(labels or [])]).lower()
    if any(token in text for token in ("bug", "error", "500")):
        return "Bug"
    if any(token in text for token in ("docs", "readme", "文档")):
        return "Documentation"
    return "Feature"


def summarize_issues(issues: list[dict]) -> dict:
    opened = [issue for issue in issues if issue.get("state") == "open"]
    counts = Counter(
        classify_issue(issue["title"], issue.get("labels"))
        for issue in opened
    )
    return {
        "total": len(issues),
        "open": len(opened),
        "closed": sum(issue.get("state") == "closed" for issue in issues),
        "categories": dict(counts),
    }


@dataclass
class Issue:
    number: int
    title: str
    labels: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"#{self.number} {self.title}"


async def analyze_locally(issue: dict) -> dict:
    # 只演示协程调度，不发真实 HTTP 请求。
    await asyncio.sleep(0)
    return {
        "number": issue["number"],
        "category": classify_issue(issue["title"], issue.get("labels")),
    }


async def analyze_all(issues: list[dict]) -> list[dict]:
    return await asyncio.gather(*(analyze_locally(issue) for issue in issues))


def main() -> None:
    print("1. 字典与空值")
    first = SAMPLE_ISSUES[0]
    print(first["title"])
    print(repr(first.get("body") or ""))

    print("\n2. 推导式与统计")
    print(json.dumps(summarize_issues(SAMPLE_ISSUES), ensure_ascii=False, indent=2))

    print("\n3. 类与方法")
    issue = Issue(number=101, title=first["title"], labels=["bug"])
    print(issue.summary())

    print("\n4. 异步并发（本地模拟）")
    print(asyncio.run(analyze_all(SAMPLE_ISSUES)))

    print("\n5. 路径与 JSON")
    print(f"脚本所在目录：{Path(__file__).resolve().parent.name}")
    text = json.dumps(SAMPLE_ISSUES, ensure_ascii=False)
    restored = json.loads(text)
    print(f"JSON 往返后共有 {len(restored)} 条 Issue")


if __name__ == "__main__":
    main()
