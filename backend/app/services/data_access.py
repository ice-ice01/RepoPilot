"""Ownership rules shared by HTTP handlers, ORM queries and background jobs.

Only authenticated request sessions are scoped. Internal jobs open their own session
and operate on IDs already authorized when the job was submitted.
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import event, inspect, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from app.db import models


def bind_user(db: Session, user: models.User) -> None:
    existing = db.info.get("auth_user_id")
    if existing is not None and existing != user.id:
        raise RuntimeError("A database session cannot switch authenticated users")
    db.info["auth_user_id"] = user.id
    db.info["auth_user"] = user


def session_user(db: Session) -> models.User:
    user = db.info.get("auth_user")
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="请先登录", headers={"WWW-Authenticate": "Bearer"})
    return user


def _ownership_rules(user_id: UUID):
    repo_ids = select(models.Repository.__table__.c.id).where(
        models.Repository.__table__.c.owner_user_id == user_id,
        # 软删除的仓库及其派生数据对普通查询隐藏；恢复/清理走专门的 Core SQL 路径。
        models.Repository.__table__.c.deleted_at.is_(None),
    )
    pr_ids = select(models.PullRequest.__table__.c.id).where(models.PullRequest.__table__.c.repo_id.in_(repo_ids))
    workflow_ids = select(models.AgentWorkflowRun.__table__.c.id).where(
        models.AgentWorkflowRun.__table__.c.repo_id.in_(repo_ids)
    )
    for mapper in models.Base.registry.mappers:
        model = mapper.class_
        if model is models.Repository:
            from sqlalchemy import and_
            yield model, and_(model.owner_user_id == user_id, model.deleted_at.is_(None))
        elif hasattr(model, "owner_user_id"):
            yield model, model.owner_user_id == user_id
        elif model is models.Workspace:
            yield model, model.created_by == user_id
        elif model is models.AuditLog:
            yield model, or_(model.user_id == user_id, model.repo_id.in_(repo_ids))
        elif hasattr(model, "repo_id"):
            yield model, model.repo_id.in_(repo_ids)
        elif model in (models.PRFile, models.PRReviewComment):
            yield model, model.pr_id.in_(pr_ids)
        elif model is models.AgentTaskRun:
            yield model, model.workflow_id.in_(workflow_ids)
        elif model is models.AuthSession:
            yield model, model.user_id == user_id


# 全局钩子：任何session执行orm语句都触发
# 在该项目中，是bind_user(db, user) 把user_id写进db.info,触发了这个全局orm事件，
# 就会自动给之后所有 SELECT/UPDATE/DELETE 追加属主过滤（owner_user_id / created_by / repo_id 链），实现多用户数据隔离；
@event.listens_for(Session, "do_orm_execute")
def scope_queries(state) -> None:
    user_id = state.session.info.get("auth_user_id") # 只读当前这个 Session的私有标识
    if user_id is None or not (state.is_select or state.is_update or state.is_delete):
        return
    # This applies to get/query/select, relationship loads, counts and ORM bulk DML.
    # Core subqueries deliberately avoid recursive loader criteria on Repository.
    for model, condition in _ownership_rules(user_id):
        state.statement = state.statement.options(
            with_loader_criteria(model, condition, include_aliases=True, propagate_to_loaders=False)
        )


@event.listens_for(Session, "before_flush")
def validate_writes(db: Session, _context, _instances) -> None:
    user_id = db.info.get("auth_user_id")
    if user_id is None:
        return
    table_models = {mapper.local_table.name: mapper.class_ for mapper in models.Base.registry.mappers}
    with db.no_autoflush:
        for obj in set(db.new) | set(db.dirty) | set(db.deleted):
            state = inspect(obj)
            if hasattr(obj, "owner_user_id"):
                owner = obj.owner_user_id
                if owner is None and obj in db.new:
                    obj.owner_user_id = user_id
                elif owner != user_id:
                    raise HTTPException(status_code=404, detail="资源不存在")
            if isinstance(obj, models.Workspace):
                if obj in db.new and obj.created_by is None:
                    obj.created_by = user_id
                if obj.created_by != user_id:
                    raise HTTPException(status_code=404, detail="资源不存在")
                for repo_id in obj.repo_ids or []:
                    if db.get(models.Repository, UUID(str(repo_id))) is None:
                        raise HTTPException(status_code=404, detail="仓库不存在")
            # Reject forged foreign keys as well as reads of another user's data.
            for column in state.mapper.columns:
                value = getattr(obj, column.key)
                if value is None or not column.foreign_keys:
                    continue
                if obj not in db.new and not state.attrs[column.key].history.has_changes():
                    continue
                for foreign_key in column.foreign_keys:
                    target = table_models.get(foreign_key.column.table.name)
                    if target is models.User:
                        if isinstance(obj, (models.AuthBootstrap, models.AuthSession)):
                            continue
                        if value != user_id:
                            raise HTTPException(status_code=404, detail="用户不存在")
                    elif target is not None and db.get(target, value) is None:
                        pending = any(isinstance(row, target) and row.id == value for row in db.new)
                        if not pending:
                            raise HTTPException(status_code=404, detail="关联资源不存在")
