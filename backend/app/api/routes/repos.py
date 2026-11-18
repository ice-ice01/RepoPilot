import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import ActionDraft, AgentRun, AgentTaskRun, AgentWorkflowRun, AnalysisResult, AuditLog, ChatFeedback, ChatMessage, ChatSession, CodeRelation, CodeSymbol, Conversation, ConversationMemory, Document, EvidenceItem, Issue, KnowledgeBaseConfig, KnowledgeGraphEdge, KnowledgeSourceDocument, MemoryCandidate, PRFile, PRReviewComment, ProjectIndexState, PullRequest, RecallEvent, Repository, RetrievalTestRun, ThreadMemory, WorkflowRun, Workspace
from app.db.session import get_db
from app.schemas.repos import RepoConnectRequest, RepoConnectResponse, RepoResponse, RepoSyncRequest, RepoSyncResponse
from app.core.config import settings
from app.core.security import encrypt_token
from app.services.github.client import GitHubClient
from app.services.code_analysis import sync_repository_code_analysis_by_id
from app.services.rag.vector_store import delete_milvus_documents
from app.services.repos_sync import RepoSyncInProgressError, sync_repository
from app.services.data_access import session_user
from app.services.permissions import require_permission
from app.db.models import AuthBootstrap

router = APIRouter()

SUPPORTED_CONNECT_PROVIDERS = {"github", "github_compatible"}
GITHUB_HOSTS = {"github.com", "www.github.com"}


@dataclass(frozen=True)
class GitRemoteInfo:
    owner: str
    repo: str
    clone_url: str
    host: str | None = None
    default_branch: str | None = None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _resolve_user_path(raw_path: str, field_name: str) -> Path:
    cleaned = raw_path.strip().strip('"').strip("'")
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty.")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _git_output(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="未找到 git 可执行文件，无法检查本地仓库。") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise HTTPException(status_code=400, detail=f"Failed to inspect local repository: {detail}") from exc
    return result.stdout.strip()


def _parse_git_remote_url(remote_url: str) -> GitRemoteInfo:
    value = remote_url.strip()
    host: str | None = None
    path = ""
    scp_like = re.match(r"^(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$", value)

    if scp_like and "://" not in value:
        host = scp_like.group("host")
        path = scp_like.group("path")
    else:
        parsed = urlparse(value)
        host = parsed.hostname or parsed.netloc or None
        path = parsed.path

    parts = [item.strip() for item in path.replace("\\", "/").strip("/").split("/") if item.strip()]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")

    owner = parts[-2]
    repo = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")
    return GitRemoteInfo(owner=owner, repo=repo, clone_url=value, host=host.lower() if host else None)


def _inspect_local_repository(raw_path: str) -> tuple[Path, GitRemoteInfo]:
    path = _resolve_user_path(raw_path, "local_path")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Local repository path does not exist or is not a directory: {path}")

    inside_work_tree = _git_output(["rev-parse", "--is-inside-work-tree"], path).lower()
    if inside_work_tree != "true":
        raise HTTPException(status_code=400, detail=f"Local path is not inside a git repository: {path}")

    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"], path)).resolve()
    remote_url = _git_output(["remote", "get-url", "origin"], repo_root)
    remote_info = _parse_git_remote_url(remote_url)
    current_branch = _git_output(["branch", "--show-current"], repo_root) or None
    return repo_root, GitRemoteInfo(
        owner=remote_info.owner,
        repo=remote_info.repo,
        clone_url=remote_info.clone_url,
        host=remote_info.host,
        default_branch=current_branch,
    )


def _infer_provider(requested_provider: str | None, remote_host: str | None) -> str:
    provider = requested_provider or "github"
    if remote_host and remote_host not in GITHUB_HOSTS and provider == "github":
        return "github_compatible"
    return provider


def _effective_api_base_url(provider: str, raw_base_url: str | None, remote_host: str | None) -> str | None:
    if provider == "github":
        return "https://api.github.com"
    base_url = _clean_text(raw_base_url)
    if base_url:
        return base_url.rstrip("/")
    if remote_host and remote_host not in GITHUB_HOSTS:
        return f"https://{remote_host}/api/v3"
    return None


def _clone_target_from_parent(raw_parent: str, repo_name: str) -> Path:
    parent = _resolve_user_path(raw_parent, "clone_parent_dir")
    if parent.exists() and not parent.is_dir():
        raise HTTPException(status_code=400, detail=f"Download parent path is not a directory: {parent}")

    target = (parent / repo_name).resolve()
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Download target exists and is not a directory: {target}")
    if target.exists() and not (target / ".git").exists() and any(target.iterdir()):
        raise HTTPException(
            status_code=400,
            detail=f"Download target already exists and is not an empty git repository: {target}",
        )
    return target


# 用户连接仓库后进行首次同步
@router.post("/connect", response_model=RepoConnectResponse)
async def connect_repo(
    payload: RepoConnectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _perm: object = Depends(require_permission("repo:create")),
) -> RepoConnectResponse:
    # 这里相当于 Spring MVC 的 Controller：接收前端连接请求，并负责编排“校验仓库 -> 保存配置 -> 首次同步”。
    local_path = _clean_text(payload.local_path)
    clone_parent_dir = _clean_text(payload.clone_parent_dir)
    owner = _clean_text(payload.owner)
    repo_name = _clean_text(payload.repo)
    requested_provider = payload.provider or "github"
    token = payload.token or payload.github_token
    current_user = session_user(db)
    if local_path or clone_parent_dir or payload.api_base_url:
        bootstrap = db.get(AuthBootstrap, 1)
        if bootstrap is None or bootstrap.claimed_by != current_user.id:
            raise HTTPException(status_code=403, detail="只有初始化管理员可以配置服务器目录或自定义 GitHub API 地址")
    checkout_path: Path | None = None
    checkout_mode = "managed"
    remote_info: GitRemoteInfo | None = None

    if local_path:
        # 本地仓库检查要跑多次 git 子进程，放线程池避免阻塞事件循环。
        checkout_path, remote_info = await asyncio.to_thread(_inspect_local_repository, local_path)
        owner = remote_info.owner
        repo_name = remote_info.repo
        checkout_mode = "local"
    elif not owner or not repo_name:
        raise HTTPException(status_code=400, detail="Provide either a local repository path or owner/repository.")

    provider = _infer_provider(requested_provider, remote_info.host if remote_info else None)
    base_url = _effective_api_base_url(provider, payload.api_base_url, remote_info.host if remote_info else None)
    if provider not in SUPPORTED_CONNECT_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"{provider} real connection is not implemented yet. Use demo mode or github_compatible with a GitHub-compatible REST API.",
        )

    if not local_path and clone_parent_dir:
        checkout_path = _clone_target_from_parent(clone_parent_dir, repo_name or "repository")

    full_name = f"{owner}/{repo_name}"
    effective_demo_mode = payload.demo_mode
    fallback_message: str | None = None
    repo_data = {
        "id": None,
        "description": "Demo repository for DevFlow AI",
        "default_branch": remote_info.default_branch if remote_info and remote_info.default_branch else "main",
        "clone_url": remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git",
    }
    if not payload.demo_mode:
        try:
            # 先调用 GitHub REST API 验证仓库可访问；token 只用于请求，不直接返回给前端。
            repo_data = await GitHubClient(token, base_url).get_repo(owner or "", repo_name or "")
        except Exception as exc:
            if token or not settings.demo_mode:
                raise HTTPException(status_code=400, detail=f"Repository connection failed: {exc}") from exc
            effective_demo_mode = True
            fallback_message = (
                f"GitHub verification failed without a token: {exc}. "
                "Added the repository in local cache mode; add a token or retry later for full GitHub sync."
            )

    repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        # 归属过滤会隐藏软删除行：同归属 + 同名仓库若处于软删除保留期，直接复活复用，
        # 否则唯一约束 (owner_user_id, full_name) 会让插入重复行失败。
        from sqlalchemy import select, update as sql_update

        soft_deleted_id = db.execute(
            select(Repository.__table__.c.id).where(
                Repository.__table__.c.full_name == full_name,
                Repository.__table__.c.owner_user_id == current_user.id,
                Repository.__table__.c.deleted_at.isnot(None),
            )
        ).scalar_one_or_none()
        if soft_deleted_id is not None:
            db.execute(
                sql_update(Repository.__table__)
                .where(Repository.__table__.c.id == soft_deleted_id)
                .values(deleted_at=None)
            )
            db.expire_all()
            repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        repo = Repository(owner=owner or "", name=repo_name or "", full_name=full_name)
        db.add(repo)
    repo.owner = owner or repo.owner
    repo.name = repo_name or repo.name
    repo.provider = provider
    repo.api_base_url = base_url
    if token:
        repo.github_token_encrypted = encrypt_token(token, settings.token_encryption_key)
    repo.description = repo_data.get("description")
    repo.default_branch = repo_data.get("default_branch")
    repo.clone_url = repo_data.get("clone_url") or (remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git")
    repo.local_path = str(checkout_path) if checkout_path else None
    repo.checkout_mode = checkout_mode
    repo.github_id = repo_data.get("id")
    db.commit()
    db.refresh(repo)
    sync_result: RepoSyncResponse | None = None
    if not effective_demo_mode:
        # await 会等待协作数据同步完成再返回 HTTP 响应，不等于启动后台任务。
        # 首次同步走统一 Service；后面的手动刷新、Webhook、定时任务也会复用这个 Service。
        try:
            sync_result = await sync_repository(
                db,
                repo,
                RepoSyncRequest(
                    sync_issues=payload.sync_issues,
                    sync_pull_requests=payload.sync_pull_requests,
                    sync_workflow_runs=payload.sync_workflow_runs,
                    limit=payload.initial_sync_limit,
                ),
                trigger="connect",
            )
        except RepoSyncInProgressError as exc:
            raise HTTPException(status_code=409, detail="该仓库已有同步正在进行，请稍后刷新查看结果。") from exc
        # 源码分析是另一条链路：响应发送后执行，并不在这里自动生成协作数据的 RAG 索引。
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    elif fallback_message:
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    return RepoConnectResponse(
        repo_id=repo.id,
        full_name=repo.full_name,
        provider=repo.provider,
        api_base_url=repo.api_base_url,
        clone_url=repo.clone_url,
        local_path=repo.local_path,
        checkout_mode=repo.checkout_mode,
        default_branch=repo.default_branch,
        connected=True,
        demo_mode=effective_demo_mode,
        message=(
            fallback_message
            or (
                "Demo mode only created a local repository record."
                if effective_demo_mode
                else "Repository connection verified. Initial Issue/PR/CI sync completed; code checkout and graph analysis started."
            )
        ),
        sync_status=sync_result.status if sync_result else ("fallback" if fallback_message else "skipped"),
        synced=sync_result.synced if sync_result else {"issues": 0, "pull_requests": 0, "workflow_runs": 0},
    )


@router.get("", response_model=list[RepoResponse])
async def list_repos(db: Session = Depends(get_db)) -> list[Repository]:
    return (
        db.query(Repository)
        .filter(Repository.provider != "knowledge_base")
        .order_by(Repository.updated_at.desc())
        .all()
    )


# 手动调用仓库同步接口
@router.post("/{repo_id}/sync", response_model=RepoSyncResponse)
async def sync_repo(
    repo_id: UUID,
    payload: RepoSyncRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _perm: object = Depends(require_permission("repo:sync")),
) -> RepoSyncResponse:
    # 手动刷新入口只负责找到 Repository，实际的远端抓取和数据库 upsert 在 repos_sync.py。
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    # 等协作数据提交后再返回；源码 checkout/分析由响应后的后台任务单独执行。
    try:
        result = await sync_repository(db, repo, payload)
    except RepoSyncInProgressError as exc:
        raise HTTPException(status_code=409, detail="该仓库已有同步正在进行（可能是 Webhook 或定时任务触发），请稍后重试。") from exc
    background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    return result


@router.post("/{repo_id}/select")
async def select_repo(
    repo_id: UUID,
    db: Session = Depends(get_db),
    _perm: object = Depends(require_permission("repo:read")),
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    repo.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"repo_id": str(repo.id), "full_name": repo.full_name, "selected": True}


@router.delete("/{repo_id}")
async def delete_repo(
    repo_id: UUID,
    db: Session = Depends(get_db),
    user: object = Depends(require_permission("settings:manage")),
) -> dict:
    """软删除仓库：立即从各页面隐藏，保留期内可恢复，到期由调度器彻底清理。"""
    from app.services.permissions import write_audit_log as _audit
    from app.services.repo_lifecycle import soft_delete_repository

    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    deleted_at = soft_delete_repository(db, repo)
    _audit(
        db,
        user=user,  # type: ignore[arg-type]
        repo_id=repo.id,
        action="repo.soft_delete",
        target_type="repository",
        target_id=str(repo.id),
    )
    db.commit()
    return {
        "repo_id": str(repo_id),
        "deleted": True,
        "soft_deleted": True,
        "deleted_at": deleted_at.isoformat(),
        "retention_hours": settings.repo_soft_delete_retention_hours,
        "message": (
            f"仓库已删除，{settings.repo_soft_delete_retention_hours} 小时内可在已删除仓库列表中恢复；"
            "到期后将被彻底清理。"
        ),
    }


@router.get("/deleted")
async def list_deleted_repos(
    db: Session = Depends(get_db),
    user: object = Depends(require_permission("repo:read")),
) -> list[dict]:
    """保留期内的软删除仓库列表，供恢复/彻底删除界面使用。"""
    from sqlalchemy import select

    bootstrap = db.get(AuthBootstrap, 1)
    is_admin = bootstrap is not None and bootstrap.claimed_by == user.id  # type: ignore[union-attr]
    statement = select(
        Repository.__table__.c.id,
        Repository.__table__.c.full_name,
        Repository.__table__.c.deleted_at,
    ).where(Repository.__table__.c.deleted_at.isnot(None))
    if not is_admin:
        statement = statement.where(Repository.__table__.c.owner_user_id == user.id)
    items = []
    for row in db.execute(statement).all():
        deleted_at = row.deleted_at
        if deleted_at is None:
            continue
        from datetime import timedelta

        expires = deleted_at + timedelta(hours=settings.repo_soft_delete_retention_hours)
        items.append(
            {
                "repo_id": str(row.id),
                "full_name": row.full_name,
                "deleted_at": deleted_at.isoformat(),
                "purges_at": expires.isoformat(),
            }
        )
    return items


@router.post("/{repo_id}/restore")
async def restore_repo(
    repo_id: UUID,
    db: Session = Depends(get_db),
    user: object = Depends(require_permission("settings:manage")),
) -> dict:
    """恢复软删除仓库（Core 更新绕过归属过滤，端点内自行校验归属）。"""
    from sqlalchemy import select

    owner_user_id = db.execute(
        select(Repository.__table__.c.owner_user_id).where(Repository.__table__.c.id == repo_id)
    ).scalar_one_or_none()
    bootstrap = db.get(AuthBootstrap, 1)
    is_admin = bootstrap is not None and bootstrap.claimed_by == user.id  # type: ignore[union-attr]
    if owner_user_id is None or (owner_user_id != user.id and not is_admin):  # type: ignore[union-attr]
        raise HTTPException(status_code=404, detail="未找到仓库")
    from app.services.repo_lifecycle import restore_repository_row

    if not restore_repository_row(db, repo_id):
        raise HTTPException(status_code=400, detail="该仓库不在软删除状态")
    db.commit()
    return {"repo_id": str(repo_id), "restored": True}


@router.post("/{repo_id}/purge")
def purge_repo(
    repo_id: UUID,
    db: Session = Depends(get_db),
    user: object = Depends(require_permission("settings:manage")),
) -> dict:
    """彻底删除软删除仓库的全部本地数据；只允许对已软删除的仓库执行。"""
    from sqlalchemy import select

    owner_user_id = db.execute(
        select(Repository.__table__.c.owner_user_id).where(Repository.__table__.c.id == repo_id)
    ).scalar_one_or_none()
    bootstrap = db.get(AuthBootstrap, 1)
    is_admin = bootstrap is not None and bootstrap.claimed_by == user.id  # type: ignore[union-attr]
    if owner_user_id is None or (owner_user_id != user.id and not is_admin):  # type: ignore[union-attr]
        raise HTTPException(status_code=404, detail="未找到仓库")
    # 归属过滤会隐藏软删除行：用独立未绑定会话执行物理清理。
    from app.db.session import SessionLocal
    from app.services.repo_lifecycle import purge_repository

    purge_db = SessionLocal()
    try:
        repo = purge_db.get(Repository, repo_id)
        if repo is None or repo.deleted_at is None:
            raise HTTPException(status_code=400, detail="只有已删除（软删除）的仓库可以彻底清理")
        result = purge_repository(purge_db, repo)
    finally:
        purge_db.close()
    return result


@router.get("/{repo_id}/sync-runs")
async def list_sync_runs(
    repo_id: UUID,
    limit: int = 10,
    db: Session = Depends(get_db),
    _perm: object = Depends(require_permission("repo:read")),
) -> list[dict]:
    """最近同步执行记录：状态、处理/跳过条数、GitHub 限流余量。"""
    from app.db.models import SyncRun

    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    bounded = max(1, min(limit, 50))
    runs = (
        db.query(SyncRun)
        .filter(SyncRun.repo_id == repo_id)
        .order_by(SyncRun.started_at.desc())
        .limit(bounded)
        .all()
    )
    return [
        {
            "id": str(run.id),
            "trigger": run.trigger,
            "status": run.status,
            "error": run.error,
            "synced": run.synced or {},
            "skipped_unchanged": run.skipped_unchanged or {},
            "rate_limit": run.rate_limit,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for run in runs
    ]


@router.post("/{repo_id}/rate-limit")
async def repo_rate_limit(
    repo_id: UUID,
    db: Session = Depends(get_db),
    _perm: object = Depends(require_permission("repo:read")),
) -> dict:
    """实时查询该仓库 Token 的 GitHub API 限流余量。"""
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    from app.core.security import decrypt_token
    from app.services.github.client import GitHubClient

    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    client = GitHubClient(token=token, base_url=repo.api_base_url)
    try:
        payload = await client.get("/rate_limit")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"GitHub 限流信息获取失败：{exc}") from exc
    resources = payload.get("resources") or {}
    core = resources.get("core") or {}
    actions = resources.get("actions") or {}
    return {
        "repo_id": str(repo_id),
        "core": {"limit": core.get("limit"), "remaining": core.get("remaining"), "reset": core.get("reset")},
        "actions": {"limit": actions.get("limit"), "remaining": actions.get("remaining"), "reset": actions.get("reset")},
    }


@router.get("/{repo_id}/sync-status")
async def sync_status(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return {
        "repo_id": str(repo.id),
        "status": "completed" if repo.last_sync_at else "not_started",
        "last_sync_at": repo.last_sync_at,
        "message": "最近一次同步已成功完成。" if repo.last_sync_at else "仓库还没有同步过。",
    }
