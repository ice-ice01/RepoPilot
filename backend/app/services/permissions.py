from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.models import AuditLog, User
from app.db.session import get_db
from app.services.auth import get_current_user
from app.services.data_access import session_user

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"repo:read", "repo:create", "repo:sync", "agent:run", "draft:create", "draft:approve", "eval:read", "settings:manage"},
    "maintainer": {"repo:read", "repo:create", "repo:sync", "agent:run", "draft:create", "draft:approve", "eval:read"},
    "developer": {"repo:read", "repo:create", "agent:run", "draft:create", "eval:read"},
    "viewer": {"repo:read", "eval:read"},
}


def ensure_demo_user(db: Session) -> User:
    """Compatibility name; anonymous requests never create or impersonate users."""
    return session_user(db)


def has_permission(user: User, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get((user.role or "viewer").lower(), set())


def require_permission(permission: str) -> Callable:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"需要权限：{permission}")
        return user

    return dependency


async def require_api_access(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Protect every business router, including endpoints without local dependencies."""
    path, method = request.url.path, request.method
    if method in {"GET", "HEAD", "OPTIONS"}:
        permission = "eval:read" if path.startswith("/api/evals") else "repo:read"
    elif path.startswith("/api/action-drafts"):
        permission = "draft:approve" if path.endswith("/confirm") else "draft:create"
    elif path.startswith("/api/chat/feedback"):
        permission = "settings:manage" if method == "PATCH" else "repo:read"
    elif path == "/api/repos/connect":
        permission = "repo:create"
    elif path.startswith("/api/repos/") and path.endswith("/sync"):
        permission = "repo:sync"
    elif path.startswith("/api/repos/") and (path.endswith("/restore") or path.endswith("/purge")):
        permission = "settings:manage"
    elif path.startswith("/api/repos/") and path.endswith("/select"):
        permission = "repo:read"
    elif method == "DELETE" and path.startswith("/api/repos/") and "/conversations/" not in path:
        permission = "settings:manage"
    else:
        permission = "agent:run"
    if not has_permission(user, permission):
        raise HTTPException(status_code=403, detail=f"需要权限：{permission}")
    from app.db import models

    # Authorize IDs before entering vector search, filesystem tools, background
    # submission or any other service that may use a separate database session.
    identifier_sets = [dict(request.query_params), request.path_params]
    if method not in {"GET", "HEAD", "OPTIONS"} and "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            identifier_sets.append(body)
    resource_types = {
        "repo_id": models.Repository, "conversation_id": models.Conversation,
        "issue_id": models.Issue, "pr_id": models.PullRequest,
        "run_id": models.AgentRun if path.startswith("/api/chat/feedback/trace/") else models.WorkflowRun,
        "eval_id": models.EvalRun, "draft_id": models.ActionDraft, "workspace_id": models.Workspace,
        "feedback_id": models.ChatFeedback, "document_id": models.KnowledgeSourceDocument,
        "assistant_message_id": models.ChatMessage,
    }
    # Check every source independently: JSON must not mask a path/query ID.
    for identifiers in identifier_sets:
        for name, model in resource_types.items():
            raw_id = identifiers.get(name)
            if not raw_id:
                continue
            try:
                resource_id = UUID(str(raw_id))
            except (ValueError, TypeError):
                continue  # The endpoint's typed schema returns the validation error.
            if db.get(model, resource_id) is None:
                # 软删除的仓库会被归属过滤隐藏，但 restore/purge 正是要操作这些行，
                # 由端点自身用 Core SQL 校验存在性与归属。
                soft_delete_endpoint = model is models.Repository and (path.endswith("/restore") or path.endswith("/purge"))
                if not soft_delete_endpoint:
                    raise HTTPException(status_code=404, detail="资源不存在")
        repo_ids = identifiers.get("repo_ids")
        for raw_id in repo_ids if isinstance(repo_ids, list) else []:
            try:
                resource_id = UUID(str(raw_id))
            except (ValueError, TypeError):
                continue
            if db.get(models.Repository, resource_id) is None:
                raise HTTPException(status_code=404, detail="仓库不存在")
    return user


def write_audit_log(
    db: Session,
    *,
    user: User | None,
    repo_id=None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    status: str = "success",
    request_json: dict | None = None,
    result_json: dict | None = None,
) -> AuditLog:
    from app.core.logging import current_request_id

    row = AuditLog(
        user_id=user.id if user else None,
        repo_id=repo_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        status=status,
        request_id=current_request_id(),
        request_json=request_json or {},
        result_json=result_json or {},
    )
    db.add(row)
    db.flush()
    return row
