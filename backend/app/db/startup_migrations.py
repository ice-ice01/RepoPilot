"""启动期数据迁移：手工 SQL 建唯一索引前先清理历史重复行。

`Base.metadata.create_all` 只对新建表生效，已有表不会自动获得 models.py 里新加的
UniqueConstraint，因此这里按 (repo_id, github_id) 去重后显式 `CREATE UNIQUE INDEX IF NOT EXISTS`。
多 worker 部署时整个启动迁移由 PostgreSQL 咨询锁串行化，避免两个进程同时改表。
"""

import logging
from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_STARTUP_LOCK_KEY = "devflow_startup_migrations"

# (表名, 远端 ID 列名, 唯一索引名)
_UNIQUE_SYNC_TABLES = [
    ("issues", "github_issue_id", "uq_issues_repo_github_issue"),
    ("pull_requests", "github_pr_id", "uq_pull_requests_repo_github_pr"),
    ("workflow_runs", "github_run_id", "uq_workflow_runs_repo_github_run"),
]

# (表名, 列名, DDL 类型)：新版本模型新增的列，对已有库幂等补齐。
_MISSING_COLUMNS = [
    ("repositories", "deleted_at", "TIMESTAMP WITH TIME ZONE" , "ix_repositories_deleted_at"),
    ("auth_sessions", "user_agent", "VARCHAR(300)", None),
    ("audit_logs", "request_id", "VARCHAR(36)", "ix_audit_logs_request_id"),
]


def reconcile_missing_columns(engine: Engine) -> None:
    """为已有表补齐新版本模型新增的列（幂等）。"""
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, column, ddl, index_name in _MISSING_COLUMNS:
            if table not in inspector.get_table_names():
                continue
            if column in {item["name"] for item in inspector.get_columns(table)}:
                continue
            try:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                if index_name:
                    connection.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("启动迁移：%s.%s 列补齐失败（%s）", table, column, exc)


@contextmanager
def startup_migration_lock(engine: Engine):
    """PostgreSQL 下用会话级咨询锁串行化多个 worker 的启动迁移；SQLite 单文件库无需加锁。"""
    if engine.dialect.name != "postgresql":
        yield
        return
    connection = engine.connect()
    try:
        connection.execute(text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": _STARTUP_LOCK_KEY})
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": _STARTUP_LOCK_KEY})
    finally:
        connection.close()


def _delete_duplicates(connection, table: str, github_column: str) -> int:
    """保留每组 (repo_id, github_id) 中 id 最小的一行，删除其余历史重复行。"""
    if connection.dialect.name == "postgresql":
        statement = text(
            f"DELETE FROM {table} AS a USING {table} AS b "
            f"WHERE a.{github_column} IS NOT NULL AND a.{github_column} = b.{github_column} "
            f"AND a.repo_id = b.repo_id AND a.id > b.id"
        )
    else:
        statement = text(
            f"DELETE FROM {table} WHERE {github_column} IS NOT NULL AND id NOT IN "
            f"(SELECT MIN(id) FROM {table} WHERE {github_column} IS NOT NULL GROUP BY repo_id, {github_column})"
        )
    return int(connection.execute(statement).rowcount or 0)


def reconcile_unique_sync_constraints(engine: Engine) -> None:
    """为同步 upsert 依赖的三张表补建唯一索引；重复行先去重，索引创建失败只告警不阻断启动。"""
    existing_tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for table, github_column, index_name in _UNIQUE_SYNC_TABLES:
            if table not in existing_tables:
                continue
            try:
                removed = _delete_duplicates(connection, table, github_column)
                if removed:
                    logger.warning("启动迁移：%s 表清理了 %s 行历史重复数据", table, removed)
                connection.execute(
                    text(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} (repo_id, {github_column})")
                )
            except Exception as exc:  # noqa: BLE001 - 启动迁移失败不应让服务无法启动
                logger.warning("启动迁移：%s 唯一索引创建失败（%s），请人工检查重复数据", table, exc)
