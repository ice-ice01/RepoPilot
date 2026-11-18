import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.chat import _build_conversation_tools, _build_engineering_workflow_runtime
from app.core.config import settings
from app.db.models import Base, Document, PullRequest, Repository, WorkflowRun
from app.schemas.repos import RepoSyncRequest
from app.services.agents.ci_debug_agent import CIDebugAgent
from app.services.chat_memory import EvidenceStore, ContextAssembler
from app.services.ci_state import ci_evidence_state, latest_workflow_runs
from app.services.rag.indexing import index_repository_documents
from app.services.rag.qa import _context_message, answer_question, build_sources
from app.services.rag.retrieval import search_similar_documents
from app.services.repos_sync import sync_repository


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def repo(db):
    repo = Repository(owner="local", name="ci", full_name="local/ci",
                      last_sync_at=datetime.now(timezone.utc))
    db.add(repo)
    db.flush()
    return repo


def add_run(db, repo, number, conclusion="failure", **kwargs):
    values = dict(repo_id=repo.id, github_run_id=number, workflow_id=10,
                  name="tests", head_branch="main", head_sha=f"sha-{number}",
                  event="push", run_attempt=1, status="completed", conclusion=conclusion,
                  logs_text="pytest FAILED AssertionError test_api.py" if conclusion == "failure" else None,
                  html_url=f"https://github.com/local/ci/actions/runs/{number}",
                  created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=number))
    values.update(kwargs)
    run = WorkflowRun(**values)
    db.add(run)
    db.flush()
    return run


@pytest.mark.parametrize("method", ["fulltext", "vector", "hybrid"])
def test_stale_index_is_reconciled_without_reindex(db, repo, monkeypatch, method):
    failed = add_run(db, repo, 1)
    index_repository_documents(db, repo.id, generate_embeddings=False, sync_vector_store=False)
    doc = db.query(Document).filter(Document.source_type == "workflow_run").one()
    # Success arrives after indexing; the SQL document and mocked vector are still failures.
    success = add_run(db, repo, 2, "success")
    monkeypatch.setattr("app.services.rag.retrieval.search_milvus_documents", lambda *args, **kwargs: [
        dict(id=str(doc.id), source_id=str(failed.id), source_type="workflow_run",
             title=doc.title, snippet=doc.content, metadata=doc.meta, score=0.99)
    ])
    items = search_similar_documents(db, repo.id, "pytest AssertionError", retrieval_method=method)
    assert items[0]["metadata"]["ci_evidence_state"] == "superseded_by_success"
    assert items[0]["metadata"]["latest_run"]["run_id"] == str(success.id)
    assert "已通过" in _context_message("当前 CI 怎样？", build_sources(items))
    assert db.query(Document).one().meta["conclusion"] == "failure"


@pytest.mark.parametrize("scope", [dict(head_branch="release"), dict(workflow_id=20),
                                      dict(event="pull_request"), dict(pull_request_numbers=[3])])
def test_unrelated_success_does_not_supersede_failure(db, repo, scope):
    failed = add_run(db, repo, 1)
    success = add_run(db, repo, 2, "success", **scope)
    assert ci_evidence_state(failed, [failed, success])["ci_evidence_state"] == "latest_failure"
    assert len(latest_workflow_runs([failed, success])) == 2


def test_latest_run_orders_creation_and_keeps_pending_state(db, repo):
    failed = add_run(db, repo, 1, updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    success = add_run(db, repo, 2, "success")
    pending = add_run(db, repo, 3, None, status="in_progress")
    state = ci_evidence_state(failed, [failed, success, pending])
    assert state["latest_run"]["status"] == "in_progress"
    assert state["latest_completed_run"]["conclusion"] == "success"
    assert state["ci_evidence_state"] == "historical_failure"


def test_legacy_scope_and_missing_source_are_not_claimed_resolved(db, repo):
    failed = add_run(db, repo, 1, workflow_id=None, head_branch=None)
    success = add_run(db, repo, 2, "success", workflow_id=None, head_branch=None)
    assert ci_evidence_state(failed, [failed, success])["ci_evidence_state"] == "scope_unknown"


@pytest.mark.asyncio
async def test_sync_successful_rerun_clears_logs_and_updates_scope(db, repo, monkeypatch):
    failed = add_run(db, repo, 1)
    index_repository_documents(db, repo.id, generate_embeddings=False, sync_vector_store=False)

    async def remote_runs(*args, **kwargs):
        return [dict(id=1, workflow_id=10, path=".github/workflows/tests.yml", name="tests",
                     head_branch="main", head_sha="new-sha", event="push", run_attempt=2,
                     status="completed", conclusion="success", pull_requests=[{"number": 7}],
                     created_at="2026-01-01T01:00:00Z", updated_at="2026-01-02T00:00:00Z")]

    async def jobs(*args, **kwargs):
        return []

    monkeypatch.setattr("app.services.repos_sync.list_workflow_runs", remote_runs)
    monkeypatch.setattr("app.services.repos_sync.list_workflow_run_jobs", jobs)
    await sync_repository(db, repo, RepoSyncRequest(sync_issues=False, sync_pull_requests=False))
    assert failed.logs_text is None
    assert failed.run_attempt == 2 and failed.head_sha == "new-sha"
    assert failed.pull_request_numbers == [7]
    items = search_similar_documents(db, repo.id, "pytest", retrieval_method="fulltext")
    assert items[0]["metadata"]["ci_evidence_state"] == "successful_rerun"
    index_repository_documents(db, repo.id, generate_embeddings=False, sync_vector_store=False)
    assert db.query(Document).filter(Document.source_type == "workflow_run").count() == 0


@pytest.mark.asyncio
async def test_chat_and_workflow_use_current_success(db, repo):
    # An older row outside the sync page may still lack the newly added fields.
    legacy = add_run(db, repo, 0, workflow_id=None, head_branch=None)
    add_run(db, repo, 1)
    success = add_run(db, repo, 2, "success")
    tools = _build_conversation_tools(repo, None, db)
    result = await tools["ci_debug_agent.analyze_workflow_run"]({"message": "检查当前 CI"}, {})
    assert "均已通过" in result["answer"]
    assert result["citations"][0]["id"] == str(success.id)
    context, _ = _build_engineering_workflow_runtime(repo, db, "检查当前 CI", {})
    assert context["has_failed_ci"] is False
    assert context["workflow_run_id"] == str(success.id)
    assert ci_evidence_state(legacy, [legacy, success])["ci_evidence_state"] == "scope_unknown"


@pytest.mark.asyncio
async def test_historical_debug_cannot_restore_blocking_from_llm(db, repo):
    failed = add_run(db, repo, 1)
    success = add_run(db, repo, 2, "success")

    class FakeLLM:
        async def chat_json(self, *args):
            return dict(failure_summary="CI still failing", failure_type="test",
                        is_merge_blocking=True, confidence=0.9)

    state = ci_evidence_state(failed, [failed, success])
    result = await CIDebugAgent(FakeLLM()).run(dict(name="tests", conclusion="failure", logs_text=failed.logs_text), state)
    assert result["is_merge_blocking"] is False
    assert "已通过" in result["failure_summary"]
    result = await CIDebugAgent(FakeLLM()).run(dict(name="tests", conclusion="success", logs_text=failed.logs_text), {})
    assert result["is_merge_blocking"] is False
    assert "执行失败" not in result["failure_summary"]


@pytest.mark.asyncio
async def test_full_qa_and_evidence_fallback_include_current_status(db, repo, monkeypatch):
    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "rag_llm_enabled", False)
    add_run(db, repo, 1)
    index_repository_documents(db, repo.id, generate_embeddings=False, sync_vector_store=False)
    add_run(db, repo, 2, "success")
    from app.services.rag.knowledge_base import get_or_create_config
    config = get_or_create_config(db, repo.id)
    config.retrieval_method = "fulltext"
    config.rerank_enabled = False
    config.score_threshold_enabled = False
    result = await answer_question(db, repo.id, "pytest AssertionError")
    assert "已通过" in result["answer"]
    assert result["sources"][0]["metadata"]["ci_evidence_state"] == "superseded_by_success"
    store = EvidenceStore(db)
    store.rebuild_repo(repo)
    monkeypatch.setattr("app.services.chat_memory.search_similar_documents", lambda *args, **kwargs: [])
    evidence = store.search(repo.id, None, "pytest", limit=5)
    assert evidence[0]["metadata"]["ci_evidence_state"] == "superseded_by_success"
    assembled = ContextAssembler(db).assemble(repo, None, "pytest AssertionError")
    assert "后续运行" in assembled.system_context


def test_pr_review_does_not_use_old_or_unrelated_failures(db, repo, monkeypatch):
    from app.api.routes.pull_requests import _build_pr_analysis_context
    pr = PullRequest(repo_id=repo.id, number=7, title="fix", state="open", head_branch="main")
    db.add(pr)
    add_run(db, repo, 1)
    add_run(db, repo, 2, "success")
    add_run(db, repo, 3, head_branch="release")
    monkeypatch.setattr("app.api.routes.pull_requests.search_similar_documents", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.api.routes.pull_requests.search_repository_code", lambda *args, **kwargs: [])
    _, context, counts = _build_pr_analysis_context(db, pr)
    assert context["ci_summary"]["failed_runs"] == 0
    assert counts["failed_ci"] == 0
