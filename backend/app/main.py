import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from app.api.routes import (
    action_drafts,
    auth,
    chat,
    ci,
    code_graph,
    conversations,
    evaluations,
    feedback,
    issues,
    knowledge,
    project_index,
    pull_requests,
    rag,
    repos,
    reports,
    search,
    skills,
    webhooks,
    workspaces,
)
from app.core.config import settings
from app.core.logging import RequestIdMiddleware, configure_logging
from app.core.proxy import TrustedProxyMiddleware
from app.db.models import Base
from app.db.session import engine
from app.services.scheduler import periodic_repo_sync_loop
from app.services.rag.vector_store import MilvusUnavailableError
from app.services.permissions import require_api_access
from app.db.auth_migration import migrate_auth_schema
from app.db.startup_migrations import (
    reconcile_missing_columns,
    reconcile_unique_sync_constraints,
    startup_migration_lock,
)


def _cors_origins() -> list[str]:
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


def _cors_origin_regex() -> str | None:
    if settings.app_env.lower() == "production":
        return None
    return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 课程项目默认自动建表；生产风格流程会在后续加入 Alembic 迁移。
    # 多 worker 同时启动时，schema 变更部分由 PostgreSQL 咨询锁串行化。
    with startup_migration_lock(engine):
        Base.metadata.create_all(bind=engine)
        migrate_auth_schema(engine)
        inspector = inspect(engine)
        if "users" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(80) DEFAULT 'owner'"))
        if "repositories" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("repositories")}
            with engine.begin() as connection:
                if "provider" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN provider VARCHAR(80) DEFAULT 'github'"))
                if "api_base_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN api_base_url TEXT"))
                if "clone_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN clone_url TEXT"))
                if "local_path" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN local_path TEXT"))
                if "checkout_mode" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN checkout_mode VARCHAR(80) DEFAULT 'managed'"))
                if "github_token_encrypted" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN github_token_encrypted TEXT"))
        if "workflow_runs" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
            with engine.begin() as connection:
                for name, sql_type in {
                    "workflow_id": "BIGINT", "workflow_path": "TEXT",
                    "head_branch": "VARCHAR(300)", "head_sha": "VARCHAR(80)",
                    "event": "VARCHAR(80)", "run_attempt": "INTEGER",
                    "pull_request_numbers": "JSON",
                }.items():
                    if name not in columns:
                        connection.execute(text(f"ALTER TABLE workflow_runs ADD COLUMN {name} {sql_type}"))
            if "jobs" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE workflow_runs ADD COLUMN jobs JSON"))
        for table_name in ["code_symbols", "code_relations"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                with engine.begin() as connection:
                    if "pr_id" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_id UUID"))
                    if "pr_number" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_number INTEGER"))
        for table_name in ["chat_messages", "chat_sessions", "agent_runs", "evidence_items"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                with engine.begin() as connection:
                    if "conversation_id" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN conversation_id UUID"))
        for table_name in ["thread_memories", "conversation_memories"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                with engine.begin() as connection:
                    for column_name in ["facts", "tasks", "user_preferences", "repo_context", "citations"]:
                        if column_name not in columns:
                            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} JSON"))
        if "documents" in inspector.get_table_names():
            document_columns = {column["name"] for column in inspector.get_columns("documents")}
            if "embedding" in document_columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE documents DROP COLUMN embedding"))
        # models.py 新增的唯一约束对已有表不生效：去重历史行后显式补建唯一索引。
        reconcile_unique_sync_constraints(engine)
        # 补齐新版本模型新增的列（软删除标记、会话 UA、审计请求 ID 等）。
        reconcile_missing_columns(engine)

    from app.db.session import SessionLocal
    from app.services.conversations import backfill_all_default_conversations
    from app.services.code_analysis import purge_legacy_code_documents
    from app.services.rag.vector_store import delete_milvus_documents, milvus_runtime_status

    db = SessionLocal()
    try:
        backfill_all_default_conversations(db)
        if purge_legacy_code_documents(db):
            db.commit()
        # BackgroundTasks 中的评测任务会随进程终止：把重启前遗留的 queued/running 标记为 error。
        from app.api.routes.evaluations import recover_interrupted_eval_runs

        recover_interrupted_eval_runs(db)
    finally:
        db.close()
    app.state.rag_vector_status = milvus_runtime_status()
    delete_milvus_documents(source_types=["code_file"], strict=False)
    if settings.auto_sync_enabled:
        app.state.repo_sync_task = asyncio.create_task(periodic_repo_sync_loop())

    yield

    task = getattr(app.state, "repo_sync_task", None)
    if task:
        task.cancel()
    # 释放模块级共享 HTTP 连接池（LLM 与 GitHub 客户端）。
    from app.services.github.client import close_shared_http_client as close_github_client
    from app.services.llm.client import close_shared_http_client as close_llm_client

    await close_github_client()
    await close_llm_client()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def private_api_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(MilvusUnavailableError)
    async def milvus_unavailable_handler(_request, exc: MilvusUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "service": "milvus", "status": "degraded"},
        )
    # 反向代理场景下按 TRUSTED_PROXIES 解析 X-Forwarded-For，登录 IP 限流才能区分真实客户端。
    app.add_middleware(TrustedProxyMiddleware, trusted_proxies=settings.trusted_proxies)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_origin_regex=_cors_origin_regex(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 请求 ID 放最外层：CORS/代理处理之前就生成，覆盖全部日志与后台任务上下文。
    app.add_middleware(RequestIdMiddleware)

    api = APIRouter(dependencies=[Depends(require_api_access)])
    api.include_router(repos.router, prefix="/api/repos", tags=["repos"])
    api.include_router(conversations.router, prefix="/api/repos", tags=["conversations"])
    api.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"])
    api.include_router(issues.router, prefix="/api", tags=["issues"])
    api.include_router(pull_requests.router, prefix="/api", tags=["pull_requests"])
    api.include_router(ci.router, prefix="/api", tags=["ci"])
    api.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    api.include_router(feedback.router, prefix="/api/chat/feedback", tags=["chat_feedback"])
    api.include_router(action_drafts.router, prefix="/api/action-drafts", tags=["action_drafts"])
    api.include_router(reports.router, prefix="/api/reports", tags=["reports"])
    api.include_router(search.router, prefix="/api/search", tags=["search"])
    api.include_router(skills.router, prefix="/api/skills", tags=["skills"])
    api.include_router(knowledge.router, prefix="/api/repos", tags=["knowledge"])
    api.include_router(project_index.router, prefix="/api/repos", tags=["project_index"])
    api.include_router(rag.router, prefix="/api/rag", tags=["rag"])
    api.include_router(code_graph.router, prefix="/api", tags=["code_graph"])
    api.include_router(evaluations.router, prefix="/api/evals", tags=["evaluations"])
    app.include_router(api)
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    return app


app = create_app()
