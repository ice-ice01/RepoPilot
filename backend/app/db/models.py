import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, Uuid, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), default="demo-user")
    username: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    role: Mapped[str] = mapped_column(String(80), default="owner", index=True)
    github_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # 登录端的 User Agent 摘要，供“会话管理”页识别设备。
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthBootstrap(Base):
    __tablename__ = "auth_bootstrap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner_user_id", "full_name", name="uq_repository_owner_full_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    owner: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    full_name: Mapped[str] = mapped_column(String(400), index=True)
    provider: Mapped[str] = mapped_column(String(80), default="github")
    api_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    clone_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkout_mode: Mapped[str] = mapped_column(String(80), default="managed")
    github_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    github_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 软删除标记：置位后对普通查询隐藏，保留期内可恢复，到期由调度器彻底清理。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    issues: Mapped[list["Issue"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="repo", cascade="all, delete-orphan")


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProjectIndexState(Base):
    __tablename__ = "project_index_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="missing", index=True)
    fingerprint: Mapped[str] = mapped_column(String(200), default="")
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    docs_indexed: Mapped[int] = mapped_column(Integer, default=0)
    docs_total: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    repo: Mapped[Repository] = relationship()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="默认会话")
    status: Mapped[str] = mapped_column(String(80), default="active", index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repo_id", "github_issue_id", name="uq_issues_repo_github_issue"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_issue_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(50), default="open")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repo: Mapped[Repository] = relationship(back_populates="issues")


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "github_pr_id", name="uq_pull_requests_repo_github_pr"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_pr_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(50), default="open")
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repo: Mapped[Repository] = relationship(back_populates="pull_requests")
    files: Mapped[list["PRFile"]] = relationship(back_populates="pr", cascade="all, delete-orphan")
    review_comments: Mapped[list["PRReviewComment"]] = relationship(back_populates="pr", cascade="all, delete-orphan")


class PRFile(Base):
    __tablename__ = "pr_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50))
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)

    pr: Mapped[PullRequest] = relationship(back_populates="files")


class PRReviewComment(Base):
    __tablename__ = "pr_review_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pr: Mapped[PullRequest] = relationship(back_populates="review_comments")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (UniqueConstraint("repo_id", "github_run_id", name="uq_workflow_runs_repo_github_run"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    workflow_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String(300), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    event: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pull_request_numbers: Mapped[list[int]] = mapped_column(JSON, default=list)
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(80))
    conclusion: Mapped[str | None] = mapped_column(String(80), nullable=True)
    html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    jobs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    logs_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repo: Mapped[Repository] = relationship(back_populates="workflow_runs")


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    target_type: Mapped[str] = mapped_column(String(80), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    analysis_type: Mapped[str] = mapped_column(String(100), index=True)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    # 类似JPA @Entity：关系库存完整正文/来源/metadata，向量另存Milvus；本表也含direct资料。
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeBaseConfig(Base):
    __tablename__ = "knowledge_base_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), unique=True, index=True)
    embedding_provider: Mapped[str] = mapped_column(String(80), default="qwen_local")
    embedding_model: Mapped[str] = mapped_column(String(240), default="Qwen/Qwen3-Embedding-0.6B")
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=1024)
    retrieval_method: Mapped[str] = mapped_column(String(40), default="hybrid")
    rerank_enabled: Mapped[bool] = mapped_column(default=True)
    rerank_provider: Mapped[str] = mapped_column(String(80), default="qwen_api")
    rerank_model: Mapped[str] = mapped_column(String(240), default="qwen3-rerank")
    top_k: Mapped[int] = mapped_column(Integer, default=3)
    score_threshold_enabled: Mapped[bool] = mapped_column(default=True)
    score_threshold: Mapped[float] = mapped_column(Float, default=0.8)
    chunk_size: Mapped[int] = mapped_column(Integer, default=800)
    # 上传/重试的字符参数；函数默认、旧上传、项目扫描各有自己的默认/固定值。
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class KnowledgeSourceDocument(Base):
    __tablename__ = "knowledge_source_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[str] = mapped_column(String(80), default="upload_file")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    stored_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    character_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetrievalTestRun(Base):
    __tablename__ = "retrieval_test_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CodeSymbol(Base):
    __tablename__ = "code_symbols"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    path: Mapped[str] = mapped_column(String(500), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str] = mapped_column(String(80), default="symbol", index=True)
    language: Mapped[str] = mapped_column(String(80), default="text", index=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    commit_sha: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    pr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CodeRelation(Base):
    __tablename__ = "code_relations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    source_symbol_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("code_symbols.id"), nullable=True, index=True)
    target_symbol_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("code_symbols.id"), nullable=True, index=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    target_name: Mapped[str] = mapped_column(String(300), index=True)
    relation_type: Mapped[str] = mapped_column(String(80), default="calls", index=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    pr_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ChatMessage(Base):
    # 原始聊天消息表：用户/助手正文，以及压缩管理器新增的 system 摘要边界。
    # Mapped[T] 是 Python 类型说明；mapped_column 定义数据库列；不是模型的内部记忆。
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    content: Mapped[str] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ChatSession(Base):
    # 一轮问答的封存记录，不是整个聊天。transcript_json 保存问答、已记录工具事件及诊断。
    # digest 是这一轮的短摘要；两个 message_id 可定位 ChatMessage 中的原始问答。
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    route: Mapped[str] = mapped_column(String(100), default="unknown")
    transcript_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(80), default="sealed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ThreadMemory(Base):
    # 兼容未指定 Conversation 的旧链路：按仓库保存一份结构化记忆。
    __tablename__ = "thread_memories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
    open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    tasks: Mapped[list[str]] = mapped_column(JSON, default=list)
    user_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    repo_context: Mapped[list[str]] = mapped_column(JSON, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sessions_incorporated: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConversationMemory(Base):
    # 正常聊天使用的自动会话记忆：每个 conversation_id 唯一，回答后更新完整状态。
    # 它可直接注入当前会话提示词，但不会仅因更新就变成向量知识库 Document。
    __tablename__ = "conversation_memories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
    open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    tasks: Mapped[list[str]] = mapped_column(JSON, default=list)
    user_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    repo_context: Mapped[list[str]] = mapped_column(JSON, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sessions_incorporated: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EvidenceItem(Base):
    # 可重建的检索证据，不是原始聊天记录。conversation_id 为空表示仓库公共证据。
    __tablename__ = "evidence_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class KnowledgeGraphEdge(Base):
    __tablename__ = "knowledge_graph_edges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    from_type: Mapped[str] = mapped_column(String(80), index=True)
    from_id: Mapped[str] = mapped_column(String(180), index=True)
    to_type: Mapped[str] = mapped_column(String(80), index=True)
    to_id: Mapped[str] = mapped_column(String(180), index=True)
    relation: Mapped[str] = mapped_column(String(80), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(120), default="explicit")
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RecallEvent(Base):
    # 回忆审计：哪个会话通过哪个工具、用什么条件查了哪些消息/证据。
    __tablename__ = "recall_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    query: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(120), default="project")
    mode: Mapped[str] = mapped_column(String(80), default="hybrid")
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MemoryCandidate(Base):
    # 从本轮记忆抽取的长期知识候选，默认 pending；用户批准后才转为 memory_note。
    __tablename__ = "memory_candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(80), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(120), default="session_sealer")
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    user_message: Mapped[str] = mapped_column(Text)
    route: Mapped[str] = mapped_column(String(100))
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    final_answer: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(80), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatFeedback(Base):
    __tablename__ = "chat_feedback"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    rating: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(40), default="open", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    notification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class AgentWorkflowRun(Base):
    __tablename__ = "agent_workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    final_answer: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTaskRun(Base):
    __tablename__ = "agent_task_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_workflow_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(120), index=True)
    agent_name: Mapped[str] = mapped_column(String(120), index=True)
    task_type: Mapped[str] = mapped_column(String(120), index=True)
    claim_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActionDraft(Base):
    __tablename__ = "action_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    draft_type: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(100), index=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(80), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(80), default="pending_confirmation", index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(80), default="success", index=True)
    # 触发本次操作的请求 ID，与日志/响应头中的 X-Request-ID 对应。
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SyncRun(Base):
    """每次仓库同步的执行记录：状态、处理/跳过条数与 GitHub 限流余量快照。"""

    __tablename__ = "sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    skipped_unchanged: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rate_limit: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
