import hashlib
import hmac

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

router = APIRouter()


def verify_signature(body: bytes, signature: str | None) -> None:
    # Webhooks use HMAC instead of browser sessions; fail closed without a secret.
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=503, detail="尚未配置 GitHub Webhook Secret")
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="缺少 GitHub webhook 签名")
    expected = hmac.new(settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    actual = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=401, detail="GitHub webhook 签名无效")


def find_repos_by_full_name(full_name: str) -> list[Repository]:
    db: Session = SessionLocal()
    try:
        return (
            db.query(Repository)
            .filter(Repository.full_name == full_name, Repository.deleted_at.is_(None))
            .all()
        )
    finally:
        db.close()


# webhook路由
@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    # Webhook 正文只是触发通知，并不直接写入 Issue/PR/CI 表；后台任务仍会通过 REST API 拉取。
    # BackgroundTasks 在当前应用进程、响应之后执行，不是 Kafka/RabbitMQ 这类持久任务队列。
    body = await request.body()
    # 校验X-Hub-Signature-256
    verify_signature(body, x_hub_signature_256)
    # 从payload.repository.full_name 找到系统中已连接的仓库
    payload = await request.json()
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    if not full_name:
        return {"accepted": False, "reason": "payload.repository.full_name is missing"}

    repos = find_repos_by_full_name(full_name)
    if not repos:
        return {"accepted": False, "reason": f"{full_name} is not connected"}

    # 对以下事件触发后台同步："issues", "pull_request", "workflow_run", "push", "check_suite", "check_run"
    if x_github_event in {"issues", "pull_request", "workflow_run", "push", "check_suite", "check_run"}:
        # 不论 GitHub 推送的是 Issue、PR 还是 CI 事件，最终都重新走统一同步 Service。
        # RepoSyncRequest 三个开关默认全为 True；这里没有按事件仅同步单条记录，也没有调度源码更新。
        # 冷却窗口吸收同一仓库短时间内的重复推送（重放/风暴）；进行中的同步会直接跳过。
        for repo in repos:
            background_tasks.add_task(
                sync_repository_by_id,
                repo.id,
                RepoSyncRequest(limit=settings.auto_sync_limit),
                min_interval_seconds=settings.webhook_sync_cooldown_seconds,
                trigger="webhook",
            )
        return {"accepted": True, "event": x_github_event, "repo_ids": [str(repo.id) for repo in repos], "action": "sync_scheduled"}

    return {"accepted": True, "event": x_github_event, "repo_ids": [str(repo.id) for repo in repos], "action": "ignored"}
