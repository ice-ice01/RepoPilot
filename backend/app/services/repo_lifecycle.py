"""仓库生命周期：软删除（保留期可恢复）-> 恢复或彻底清理。

软删除只标记 repositories.deleted_at，所有派生数据保留；普通查询通过
data_access 的归属过滤自动隐藏软删除仓库。保留期到期的彻底清理由调度器触发。
恢复/清理使用 Core SQL 读取软删除行，绕过 ORM 归属过滤（那里会把软删除行过滤掉）。
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    ActionDraft, AgentRun, AgentTaskRun, AgentWorkflowRun, AnalysisResult, AuditLog, ChatFeedback,
    ChatMessage, ChatSession, CodeRelation, CodeSymbol, Conversation, ConversationMemory, Document,
    EvidenceItem, Issue, KnowledgeBaseConfig, KnowledgeGraphEdge, KnowledgeSourceDocument,
    MemoryCandidate, PRFile, PRReviewComment, ProjectIndexState, PullRequest, RecallEvent,
    Repository, RetrievalTestRun, SyncRun, ThreadMemory, WorkflowRun, Workspace,
)
from app.services.rag.vector_store import delete_milvus_documents

logger = logging.getLogger(__name__)


def soft_delete_repository(db: Session, repo: Repository) -> datetime:
    """标记软删除并立即生效；返回删除时间。"""
    deleted_at = datetime.now(timezone.utc)
    repo.deleted_at = deleted_at
    return deleted_at


def restore_repository_row(db: Session, repo_id: UUID) -> bool:
    """恢复软删除的仓库（Core 更新，绕过隐藏软删除行的 ORM 过滤）。"""
    result = db.execute(
        update(Repository.__table__)
        .where(Repository.__table__.c.id == repo_id, Repository.__table__.c.deleted_at.isnot(None))
        .values(deleted_at=None)
    )
    return bool(result.rowcount)


def soft_deleted_repo_rows(db: Session, user_id: UUID | None = None) -> list[dict]:
    """列出保留期内的软删除仓库（可选按归属过滤），供恢复界面使用。"""
    statement = select(
        Repository.__table__.c.id,
        Repository.__table__.c.full_name,
        Repository.__table__.c.owner_user_id,
        Repository.__table__.c.deleted_at,
    ).where(Repository.__table__.c.deleted_at.isnot(None))
    if user_id is not None:
        statement = statement.where(Repository.__table__.c.owner_user_id == user_id)
    return [dict(row._mapping) for row in db.execute(statement).all()]


def purge_repository(db: Session, repo: Repository) -> dict:
    """物理删除仓库及其全部派生数据；调用方负责先做归属/权限校验。"""
    repo_id = repo.id
    issue_ids = [item.id for item in db.query(Issue.id).filter(Issue.repo_id == repo_id).all()]
    pr_ids = [item.id for item in db.query(PullRequest.id).filter(PullRequest.repo_id == repo_id).all()]
    run_ids = [item.id for item in db.query(WorkflowRun.id).filter(WorkflowRun.repo_id == repo_id).all()]
    conversation_ids = [item.id for item in db.query(Conversation.id).filter(Conversation.repo_id == repo_id).all()]
    session_ids = [
        item.id
        for item in db.query(ChatSession.id)
        .filter(
            or_(
                ChatSession.repo_id == repo_id,
                ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]
    workflow_ids = [
        item.id
        for item in db.query(AgentWorkflowRun.id)
        .filter(
            or_(
                AgentWorkflowRun.repo_id == repo_id,
                AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]

    for target_type, ids in [
        ("issue", issue_ids),
        ("pull_request", pr_ids),
        ("workflow_run", run_ids),
    ]:
        if ids:
            db.query(AnalysisResult).filter(
                AnalysisResult.target_type == target_type,
                AnalysisResult.target_id.in_(ids),
            ).delete(synchronize_session=False)

    knowledge_sources = (
        db.query(KnowledgeSourceDocument)
        .filter(KnowledgeSourceDocument.repo_id == repo_id)
        .all()
    )
    stored_paths = [Path(source.stored_path) for source in knowledge_sources]

    if pr_ids:
        db.query(PRFile).filter(PRFile.pr_id.in_(pr_ids)).delete(synchronize_session=False)
        db.query(PRReviewComment).filter(PRReviewComment.pr_id.in_(pr_ids)).delete(synchronize_session=False)
    db.query(Document).filter(Document.repo_id == repo_id).delete(synchronize_session=False)
    db.query(RetrievalTestRun).filter(RetrievalTestRun.repo_id == repo_id).delete(synchronize_session=False)
    db.query(KnowledgeSourceDocument).filter(KnowledgeSourceDocument.repo_id == repo_id).delete(synchronize_session=False)
    db.query(KnowledgeBaseConfig).filter(KnowledgeBaseConfig.repo_id == repo_id).delete(synchronize_session=False)
    db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo_id).delete(synchronize_session=False)
    db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo_id).delete(synchronize_session=False)
    db.query(CodeRelation).filter(CodeRelation.repo_id == repo_id).delete(synchronize_session=False)
    db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo_id).delete(synchronize_session=False)
    db.query(ActionDraft).filter(ActionDraft.repo_id == repo_id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.repo_id == repo_id).delete(synchronize_session=False)
    db.query(ChatFeedback).filter(ChatFeedback.repo_id == repo_id).delete(synchronize_session=False)
    db.query(SyncRun).filter(SyncRun.repo_id == repo_id).delete(synchronize_session=False)
    db.query(MemoryCandidate).filter(
        or_(
            MemoryCandidate.repo_id == repo_id,
            MemoryCandidate.conversation_id.in_(conversation_ids) if conversation_ids else False,
            MemoryCandidate.session_id.in_(session_ids) if session_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(RecallEvent).filter(
        or_(
            RecallEvent.repo_id == repo_id,
            RecallEvent.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    if workflow_ids:
        db.query(AgentTaskRun).filter(AgentTaskRun.workflow_id.in_(workflow_ids)).delete(synchronize_session=False)
    db.query(AgentWorkflowRun).filter(
        or_(
            AgentWorkflowRun.repo_id == repo_id,
            AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatMessage).filter(
        or_(
            ChatMessage.repo_id == repo_id,
            ChatMessage.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatSession).filter(
        or_(
            ChatSession.repo_id == repo_id,
            ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ConversationMemory).filter(
        or_(
            ConversationMemory.repo_id == repo_id,
            ConversationMemory.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ThreadMemory).filter(ThreadMemory.repo_id == repo_id).delete(synchronize_session=False)
    db.query(EvidenceItem).filter(
        or_(
            EvidenceItem.repo_id == repo_id,
            EvidenceItem.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(AgentRun).filter(
        or_(
            AgentRun.repo_id == repo_id,
            AgentRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.repo_id == repo_id).delete(synchronize_session=False)
    db.query(Issue).filter(Issue.repo_id == repo_id).delete(synchronize_session=False)
    db.query(PullRequest).filter(PullRequest.repo_id == repo_id).delete(synchronize_session=False)
    db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_id).delete(synchronize_session=False)
    for workspace in db.query(Workspace).all():
        next_repo_ids = [item for item in (workspace.repo_ids or []) if str(item) != str(repo_id)]
        if next_repo_ids != (workspace.repo_ids or []):
            workspace.repo_ids = next_repo_ids
    db.delete(repo)
    db.commit()

    milvus_deleted = delete_milvus_documents(repo_id=repo_id, strict=False)
    removed_files = 0
    file_cleanup_errors: list[str] = []
    for path in stored_paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                removed_files += 1
        except OSError as exc:
            file_cleanup_errors.append(f"{path}: {exc}")

    return {
        "repo_id": str(repo_id),
        "deleted": True,
        "cleanup": {
            "milvus_deleted": milvus_deleted,
            "removed_files": removed_files,
            "file_errors": file_cleanup_errors,
        },
    }


def purge_expired_soft_deleted_repositories() -> int:
    """清理保留期已过的软删除仓库；由调度器周期调用。"""
    from app.db.session import SessionLocal

    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.repo_soft_delete_retention_hours)
        rows = soft_deleted_repo_rows(db)
        purged = 0
        for row in rows:
            deleted_at = row["deleted_at"]
            if deleted_at.tzinfo is None:
                deleted_at = deleted_at.replace(tzinfo=timezone.utc)
            if deleted_at >= cutoff:
                continue
            repo = db.get(Repository, row["id"])
            if repo is None:
                continue
            try:
                purge_repository(db, repo)
                purged += 1
                logger.info("保留期已过，彻底清理仓库 %s", row["full_name"])
            except Exception as exc:  # noqa: BLE001 - 单个仓库清理失败不阻断其他仓库
                db.rollback()
                logger.warning("清理过期软删除仓库 %s 失败：%s", row["id"], exc)
        return purged
    finally:
        db.close()
