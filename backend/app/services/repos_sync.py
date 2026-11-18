import logging
from asyncio import Lock
from datetime import datetime, timezone
from time import monotonic
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, SyncRun, WorkflowRun
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs
from app.services.github.client import rate_limit_info_var
from app.services.github.issues import list_issues
from app.services.github.pull_requests import list_pull_request_files, list_pull_request_review_comments, list_pull_requests

logger = logging.getLogger(__name__)

# 手动刷新、Webhook、定时调度三个入口都会进入本模块；同一仓库同时只允许一个同步，
# 冷却窗口用于吸收 Webhook 重放/风暴造成的重复触发。
_repo_sync_locks: dict[UUID, Lock] = {}
_last_sync_started: dict[UUID, float] = {}


class RepoSyncInProgressError(RuntimeError):
    """同一仓库已有同步正在进行时抛出，调用方据此跳过或返回 409。"""


def _sync_lock(repo_id: UUID) -> Lock:
    # 事件循环内 dict.setdefault 之间没有 await，不会被打断，无需额外保护。
    return _repo_sync_locks.setdefault(repo_id, Lock())


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left == right


async def _fetch_list(caller, *args, **kwargs) -> list[dict] | None:
    """拉取列表；HTTP 失败返回 None 表示“本次无法确认远端数据”，调用方必须保留旧数据。"""
    try:
        return await caller(*args, **kwargs)
    except httpx.HTTPStatusError as exc:
        logger.warning("GitHub 拉取失败（%s http_%s），保留本地旧数据", getattr(caller, "__name__", caller), exc.response.status_code)
        return None


def parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(user: dict | None) -> str | None:
    return user.get("login") if user else None


async def sync_repository(db: Session, repo: Repository, payload: RepoSyncRequest, trigger: str = "manual") -> RepoSyncResponse:
    # 统一同步 Service：Controller、Webhook 和 Scheduler 都汇聚到这里。
    # 它把 GitHub 返回的 JSON 映射成 SQLAlchemy Entity，并按远端 id 做更新或新增（upsert）。
    if repo.provider not in {"github", "github_compatible"}:
        raise HTTPException(status_code=400, detail=f"{repo.provider} sync is not implemented yet")

    lock = _sync_lock(repo.id)
    if lock.locked():
        raise RepoSyncInProgressError(str(repo.id))
    async with lock:
        return await _sync_repository_locked(db, repo, payload, trigger)


async def _sync_repository_locked(
    db: Session, repo: Repository, payload: RepoSyncRequest, trigger: str
) -> RepoSyncResponse:
    _last_sync_started[repo.id] = monotonic()
    started_at = datetime.now(timezone.utc)
    sync_run = SyncRun(
        repo_id=repo.id,
        owner_user_id=repo.owner_user_id,
        trigger=trigger,
        status="running",
        started_at=started_at,
    )
    db.add(sync_run)
    db.flush()
    try:
        result = await _perform_sync(db, repo, payload, sync_run)
        return result
    except Exception as exc:
        # 同步失败也要留下可诊断的执行记录：回滚业务变更后单独提交失败记录。
        try:
            db.rollback()
            db.add(
                SyncRun(
                    repo_id=repo.id,
                    owner_user_id=repo.owner_user_id,
                    trigger=trigger,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    rate_limit=(rate_limit_info_var.get() or None),
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001 - 记录失败不能掩盖原始同步错误
            db.rollback()
        raise


async def _perform_sync(db: Session, repo: Repository, payload: RepoSyncRequest, sync_run: SyncRun) -> RepoSyncResponse:
    synced = {"issues": 0, "pull_requests": 0, "workflow_runs": 0}
    skipped_unchanged = {"pull_requests": 0, "workflow_runs": 0}
    # 先取上次同步时间，作为 Issue since 与“PR/Run 未变更则跳过明细拉取”的基准。
    previous_sync_at = repo.last_sync_at
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    base_url = repo.api_base_url

    if payload.sync_issues:
        # GitHub 的 /issues 接口会同时返回 PR，因此 list_issues() 会过滤掉带 pull_request 字段的项。
        # since 只返回上次同步后更新过的 Issue；首次同步（previous_sync_at 为空）拉全量。
        try:
            remote_issues = await list_issues(
                repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url, since=previous_sync_at
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub issues sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_issues:
            # 本地 UUID 与 GitHub id 不是一回事；repo_id + github_issue_id 用来找到同一条远端记录。
            issue = db.query(Issue).filter(Issue.repo_id == repo.id, Issue.github_issue_id == item.get("id")).one_or_none()
            if issue is None:
                issue = Issue(repo_id=repo.id, github_issue_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(issue)
            issue.number = item.get("number", issue.number)
            issue.title = item.get("title") or issue.title
            issue.body = item.get("body")
            issue.state = item.get("state") or "open"
            issue.labels = [label.get("name") for label in item.get("labels", []) if label.get("name")]
            issue.author = login_of(item.get("user"))
            issue.assignees = [login_of(user) for user in item.get("assignees", []) if login_of(user)]
            issue.created_at = parse_github_datetime(item.get("created_at"))
            issue.updated_at = parse_github_datetime(item.get("updated_at"))
            issue.closed_at = parse_github_datetime(item.get("closed_at"))
            synced["issues"] += 1

    if payload.sync_pull_requests:
        # PR 主记录、文件列表、Review 评论分别来自三个 API，但最终关联到同一个 PullRequest Entity。
        try:
            remote_prs = await list_pull_requests(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub pull requests sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_prs:
            item_updated_at = parse_github_datetime(item.get("updated_at"))
            pr = db.query(PullRequest).filter(PullRequest.repo_id == repo.id, PullRequest.github_pr_id == item.get("id")).one_or_none()
            if pr is None:
                pr = PullRequest(repo_id=repo.id, github_pr_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(pr)
                # 类似 EntityManager.flush()：先把新增 PR 写入当前事务，尚未 commit，供子记录关联。
                db.flush()
            # 远端 updated_at 与本地一致说明 PR 自上次同步后没有任何变化，跳过明细 API。
            pr_unchanged = _same_instant(item_updated_at, pr.updated_at)
            pr.number = item.get("number", pr.number)
            pr.title = item.get("title") or pr.title
            pr.body = item.get("body")
            pr.state = item.get("state") or "open"
            pr.author = login_of(item.get("user"))
            pr.base_branch = (item.get("base") or {}).get("ref")
            pr.head_branch = (item.get("head") or {}).get("ref")
            pr.merged_at = parse_github_datetime(item.get("merged_at"))
            pr.created_at = parse_github_datetime(item.get("created_at"))
            pr.updated_at = item_updated_at

            if pr_unchanged:
                synced["pull_requests"] += 1
                skipped_unchanged["pull_requests"] += 1
                continue

            # 子记录采用“先拉取、成功后再删除旧集合 -> 重新插入”；拉取失败（None）时保留旧数据，
            # 避免一次网络抖动把已同步的 diff/评论清空。
            files = await _fetch_list(list_pull_request_files, repo.owner, repo.name, pr.number, token=token, base_url=base_url)
            if files is not None:
                db.query(PRFile).filter(PRFile.pr_id == pr.id).delete()
                for file_item in files:
                    db.add(
                        PRFile(
                            pr_id=pr.id,
                            filename=file_item.get("filename") or "",
                            status=file_item.get("status") or "modified",
                            additions=file_item.get("additions") or 0,
                            deletions=file_item.get("deletions") or 0,
                            patch=file_item.get("patch"),
                        )
                    )
            review_comments = await _fetch_list(
                list_pull_request_review_comments, repo.owner, repo.name, pr.number, token=token, base_url=base_url
            )
            if review_comments is not None:
                db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).delete()
                for comment_item in review_comments:
                    db.add(
                        PRReviewComment(
                            pr_id=pr.id,
                            github_comment_id=comment_item.get("id"),
                            body=comment_item.get("body"),
                            path=comment_item.get("path"),
                            line=comment_item.get("line") or comment_item.get("original_line"),
                            author=login_of(comment_item.get("user")),
                            created_at=parse_github_datetime(comment_item.get("created_at")),
                            updated_at=parse_github_datetime(comment_item.get("updated_at")),
                        )
                    )
            synced["pull_requests"] += 1

    if payload.sync_workflow_runs:
        # CI 运行记录还会继续展开 jobs/steps；失败运行额外下载日志，供 CI Agent 和 RAG 使用。
        # 列表本身拉取失败必须显式报错：静默返回空会让“同步成功但 0 条”掩盖 GitHub 限流等故障。
        try:
            remote_runs = await list_workflow_runs(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub workflow runs sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_runs:
            item_updated_at = parse_github_datetime(item.get("updated_at"))
            run = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == item.get("id")).one_or_none()
            if run is None:
                run = WorkflowRun(repo_id=repo.id, github_run_id=item.get("id"), name=item.get("name") or "workflow")
                db.add(run)
            run_unchanged = _same_instant(item_updated_at, run.updated_at) and (item.get("run_attempt") or 0) == (run.run_attempt or 0)
            previous_attempt = run.run_attempt
            run.name = item.get("name") or run.name
            run.status = item.get("status") or "unknown"
            run.conclusion = item.get("conclusion")
            run.workflow_id = item.get("workflow_id")
            run.workflow_path = item.get("path")
            run.head_branch = item.get("head_branch")
            run.head_sha = item.get("head_sha")
            run.event = item.get("event")
            run.run_attempt = item.get("run_attempt")
            run.pull_request_numbers = [pr["number"] for pr in item.get("pull_requests", []) if pr.get("number")]
            run.html_url = item.get("html_url")
            run.created_at = parse_github_datetime(item.get("created_at"))
            run.updated_at = item_updated_at

            if run_unchanged:
                synced["workflow_runs"] += 1
                skipped_unchanged["workflow_runs"] += 1
                continue

            if run.github_run_id:
                jobs = await _fetch_list(list_workflow_run_jobs, repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                if jobs is not None:
                    run.jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "html_url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                            "steps": [
                                {
                                    "name": step.get("name"),
                                    "status": step.get("status"),
                                    "conclusion": step.get("conclusion"),
                                    "number": step.get("number"),
                                }
                                for step in job.get("steps", [])
                            ],
                        }
                        for job in jobs
                    ]
            if run.conclusion == "failure" and run.github_run_id:
                # 同一 attempt 的失败日志不会变化，只在缺失或重跑（attempt 变化）后重新下载，
                # 避免每轮同步都重复传输大体积日志。
                attempt_changed = (run.run_attempt or 0) != (previous_attempt or 0)
                if run.logs_text is None or attempt_changed:
                    try:
                        run.logs_text = await get_workflow_run_logs(repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                    except httpx.HTTPStatusError as exc:
                        logger.warning("GitHub 日志下载失败（run=%s http_%s），保留旧日志", run.github_run_id, exc.response.status_code)
            elif run.conclusion == "success":
                # A successful rerun must not retain logs from its failed attempt.
                run.logs_text = None
            synced["workflow_runs"] += 1

    repo.last_sync_at = datetime.now(timezone.utc)
    # flush 只写入当前事务，commit 才提交；synced 是处理条数，不是新增条数或历史总数。
    # 此处不调用 RAG reindex 或知识图谱 rebuild：业务表更新不代表这些派生索引已经更新。
    sync_run.status = "completed"
    sync_run.synced = dict(synced)
    sync_run.skipped_unchanged = dict(skipped_unchanged)
    sync_run.rate_limit = rate_limit_info_var.get() or None
    sync_run.finished_at = datetime.now(timezone.utc)
    db.flush()
    try:
        db.commit()
    except IntegrityError as exc:
        # 唯一约束兜底：跨进程并发同步可能双方都判断“记录不存在”后各插一条。
        # 回滚并返回 409，下一轮同步会按已存在的记录走 upsert 修复。
        db.rollback()
        raise HTTPException(status_code=409, detail="检测到并发同步冲突，请稍后重试") from exc
    return RepoSyncResponse(repo_id=repo.id, status="completed", synced=synced, skipped_unchanged=skipped_unchanged)


async def sync_repository_by_id(
    repo_id: UUID,
    payload: RepoSyncRequest,
    *,
    min_interval_seconds: float = 0,
    trigger: str = "scheduler",
) -> RepoSyncResponse | None:
    """后台入口（Webhook/调度器）。返回 None 表示因冷却或并发跳过本次同步。"""
    if min_interval_seconds > 0:
        last = _last_sync_started.get(repo_id)
        if last is not None and monotonic() - last < min_interval_seconds:
            logger.info("仓库 %s 处于同步冷却窗口（%.0fs），跳过本次触发", repo_id, min_interval_seconds)
            return None
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        repo = db.get(Repository, repo_id)
        if repo is None or repo.deleted_at is not None:
            return None
        return await sync_repository(db, repo, payload, trigger=trigger)
    except RepoSyncInProgressError:
        logger.info("仓库 %s 已有同步在进行，跳过本次触发", repo_id)
        return None
    finally:
        db.close()
