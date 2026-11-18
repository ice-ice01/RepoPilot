"""Offline regression checks for evaluation validity, provenance, and tool availability."""

import asyncio
import copy
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes import chat
from app.api.routes.evaluations import _eval_comparison
from app.core.config import settings
from app.db.models import Base, Document, EvalRun, Repository
from app.services.agents.chat_agent import ChatAgent
from app.services.evals.ragas_runner import (
    REQUIRED_RAGAS_METRICS,
    RagEvalCase,
    RagasCollectionsScorer,
    _finalize_result,
    _metric_details,
    _retrieval_stages,
    _trace_rows,
    missing_metric_names,
    rescore_rag_result,
    run_rag_quality_eval,
)
from app.services.evals.suites import DEFAULT_SUITE_DIR, list_eval_suites, load_eval_suite


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def scored_row(**updates):
    return {
        "id": "case-1", "question": "问题", "reference": "参考事实",
        "reference_outcome": "独立验收要求", "response": "有证据的回答",
        "retrieved_contexts": ["原始证据"], "retrieved_sources": [{"source_id": "source-1"}],
        "tool_calls": [], "tool_observations": [], "expected_tool_calls": [],
        "hard_rules": {"passed": True, "source_hit": True, "contexts_present": True, "answer_present": True},
        "ragas": dict.fromkeys(REQUIRED_RAGAS_METRICS, 1.0), "ragas_errors": {},
        **updates,
    }


def saved_result(row):
    return {
        "target": {"repo_id": "repo-1"},
        "evaluation_set": {"source": "fixed", "sha256": "abc"},
        "pipeline": {"top_k": 5, "scoring_version": "legacy-version"},
        "ragas": {"version": "test", "judge_model": "offline"},
        "retrieval": {}, "performance": {}, "cases": [row],
    }


class OfflineScorer:
    version = "test"
    judge_model = "offline"

    def __init__(self, scores=None):
        self.calls = []
        self.scores = scores

    async def score(self, **kwargs):
        self.calls.append(kwargs)
        return (self.scores if self.scores is not None else dict.fromkeys(REQUIRED_RAGAS_METRICS, 1.0)), {}


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), -0.1, 1.1, True, "1.0"])
def test_missing_or_invalid_scores_cannot_pass(invalid):
    good = scored_row()
    bad = scored_row(ragas={**good["ragas"], "faithfulness": invalid})
    assert missing_metric_names(bad) == {"faithfulness"}
    details = _metric_details([good, bad])["faithfulness"]
    assert details == {"score": 1.0, "threshold": settings.ragas_faithfulness_min,
                       "passed": None, "scored_cases": 1, "total_cases": 2, "complete": False}
    result = _finalize_result(rows=[good, bad], retrieval={}, performance={}, judge_requested=True,
                              scorer=OfflineScorer(), judge_error=None, repo_id="repo-1", limit=5,
                              evaluation_set={"source": "fixed", "sha256": "abc"})
    assert result["evaluation_complete"] is False
    assert result["quality_status"] == "incomplete"
    assert result["passed"] is False


def test_expected_tool_scores_are_required_and_errored_scores_excluded():
    row = scored_row(expected_tool_calls=[{"name": "read", "args": {}}])
    assert missing_metric_names(row) == {"tool_call_accuracy", "tool_call_f1"}
    row["ragas_errors"] = {"faithfulness": "timeout"}
    assert _metric_details([row])["faithfulness"]["score"] is None


@pytest.mark.asyncio
async def test_optional_tool_metric_retry_requires_valid_replacement():
    row = scored_row(expected_tool_calls=[{"name": "read", "args": {}}])
    row["ragas"].update(tool_call_accuracy=0.9, tool_call_f1=float("nan"))
    result = await rescore_rag_result(saved_result(row), eval_id="old", scorer=OfflineScorer({"tool_call_f1": 1.0}))
    assert result["cases"][0]["ragas"]["tool_call_f1"] == 1.0
    assert result["evaluation_complete"] is True


@pytest.mark.asyncio
async def test_rescore_preserves_evidence_and_only_replaces_requested_scores():
    row = scored_row(ragas_errors={"faithfulness": "TimeoutError"})
    source = saved_result(row)
    original = copy.deepcopy(source)
    scorer = OfflineScorer({"faithfulness": 0.99, "context_precision": 0.01})
    result = await rescore_rag_result(source, eval_id="old-eval", scorer=scorer)
    assert source == original
    assert scorer.calls[0]["metric_names"] == {"faithfulness"}
    assert scorer.calls[0]["reference_outcome"] == "独立验收要求"
    assert scorer.calls[0]["retrieved_contexts"] == ["原始证据"]
    assert result["cases"][0]["ragas"]["context_precision"] == 1.0
    assert result["cases"][0]["ragas_errors"] == {}
    assert result["pipeline"]["scoring_version"] == "legacy-version"


@pytest.mark.asyncio
async def test_empty_retry_does_not_clear_previous_failure():
    result = await rescore_rag_result(saved_result(scored_row(ragas_errors={"faithfulness": "timeout"})),
                                      eval_id="old", scorer=OfflineScorer({}))
    assert "faithfulness" in result["cases"][0]["ragas_errors"]
    assert result["evaluation_complete"] is False


@pytest.mark.asyncio
async def test_legacy_retry_keeps_original_outcome():
    row = scored_row(ragas_errors={"faithfulness": "timeout"})
    row.pop("reference_outcome")
    source = saved_result(row)
    source["pipeline"].pop("scoring_version")
    scorer = OfflineScorer({"faithfulness": 1.0})
    result = await rescore_rag_result(source, eval_id="old", scorer=scorer)
    assert scorer.calls[0]["reference_outcome"] == row["reference"]
    assert result["pipeline"]["scoring_version"] is None


class Metric:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def ascore(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(value=1.0, reason="Supported by the saved evidence")


def collections_scorer():
    scorer = object.__new__(RagasCollectionsScorer)
    scorer.metrics = {name: Metric() for name in REQUIRED_RAGAS_METRICS}
    scorer._messages = lambda *args: ["offline messages"]
    return scorer


def score_args():
    return dict(user_input="问题", response="回答", reference="参考事实", reference_outcome="独立任务要求",
                retrieved_contexts=["证据"], tool_calls=[], tool_observations=[], expected_tool_calls=[])


@pytest.mark.asyncio
async def test_goal_uses_outcome_and_retrieval_uses_facts_with_reason_saved():
    scorer = collections_scorer()
    scores, errors = await scorer.score(**score_args())
    assert not errors and set(scores) == set(REQUIRED_RAGAS_METRICS)
    assert scorer.metrics["agent_goal_accuracy"].calls[0]["reference"] == "独立任务要求"
    assert scorer.metrics["context_recall"].calls[0]["reference"] == "参考事实"
    assert scorer.last_details["context_precision"]["reason"].startswith("Supported")


@pytest.mark.asyncio
async def test_http_timeout_classified_and_other_scores_retained():
    scorer = collections_scorer()
    scorer.metrics["context_precision"] = Metric(httpx.ReadTimeout("slow judge"))
    scores, errors = await scorer.score(**score_args())
    assert "context_precision" not in scores and len(scores) == 4
    assert "metric budget=" in errors["context_precision"]
    assert scorer.last_details["context_precision"]["error_kind"] == "timeout"


@pytest.mark.asyncio
async def test_metric_timeout_is_bounded_and_cancelled(monkeypatch):
    monkeypatch.setattr(settings, "ragas_metric_timeout_seconds", 0.01)
    cancelled = asyncio.Event()

    class HangingMetric:
        async def ascore(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    scorer = collections_scorer()
    scorer.metrics["context_precision"] = HangingMetric()
    scores, errors = await scorer.score(**score_args(), metric_names={"context_precision"})
    assert not scores and "context_precision" in errors
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_whole_metric_budget_and_concurrency_are_separate(monkeypatch):
    monkeypatch.setattr(settings, "ragas_metric_concurrency", 2)
    monkeypatch.setattr(settings, "ragas_metric_timeout_seconds", 42)
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 7)
    scorer = collections_scorer()
    active = 0
    maximum = 0
    budgets = []
    real_wait_for = asyncio.wait_for

    class SlowMetric:
        async def ascore(self, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return SimpleNamespace(value=1.0, reason=None)

    async def record_budget(pending, timeout):
        budgets.append(timeout)
        return await real_wait_for(pending, timeout=timeout)

    scorer.metrics = dict.fromkeys(REQUIRED_RAGAS_METRICS, SlowMetric())
    monkeypatch.setattr(asyncio, "wait_for", record_budget)
    await scorer.score(**score_args())
    assert maximum == 2
    assert budgets == [42] * len(REQUIRED_RAGAS_METRICS)


def test_comparison_counts_semantic_regression_and_rejects_invalid_baselines():
    base_result = saved_result(scored_row())
    candidate_result = copy.deepcopy(base_result)
    candidate_result["cases"][0]["ragas"]["faithfulness"] = 0.1
    comparison = _eval_comparison(EvalRun(result_json=base_result), EvalRun(result_json=candidate_result))
    assert comparison["comparable"] is True
    assert comparison["regressed_cases"] == 1
    for part, key, value in [("evaluation_set", "sha256", None), ("evaluation_set", "source", "auto_smoke"),
                             ("target", "repo_id", "other"), ("pipeline", "scoring_version", "different")]:
        changed = copy.deepcopy(base_result)
        changed[part][key] = value
        assert _eval_comparison(EvalRun(result_json=base_result), EvalRun(result_json=changed))["comparable"] is False


def test_round_diagnostics_keep_original_order_and_aggregate_evidence():
    case = RagEvalCase(question="q", reference="r", expected_source_ids=["hit"])
    trace = {"retrievals": [
        {"stage": "prefetch", "query": "first", "contexts": ["noise"], "sources": [{"source_id": "miss"}]},
        {"stage": "agent_tool", "query": "rewritten", "contexts": ["fact"], "sources": [{"source_id": "hit"}]},
    ]}
    rounds = _retrieval_stages(case, trace, 5)
    assert [r["recall_at_k"] for r in rounds] == [0.0, 1.0]
    assert [r["query"] for r in rounds] == ["first", "rewritten"]
    contexts, sources = _trace_rows(trace)
    assert contexts == ["noise", "fact"] and len(sources) == 2


def test_bundled_suite_provenance_repo_scope_and_readiness(db):
    payload = json.loads((DEFAULT_SUITE_DIR / "wardrobe_rag_cases.json").read_text(encoding="utf-8"))
    repo = Repository(owner="ice-ice01", name="wardrobeAgent", full_name="ice-ice01/wardrobeAgent")
    other = Repository(owner="other", name="wardrobeAgent", full_name="other/wardrobeAgent")
    db.add_all([repo, other]); db.flush()
    assert list_eval_suites(db, repo.id)[0]["ready"] is False
    assert [s["source"] for s in list_eval_suites(db, other.id)] == ["auto_smoke"]
    filename = next(iter(payload["source_checksums"]))
    source = DEFAULT_SUITE_DIR.parent / "sources" / filename
    assert hashlib.sha256(source.read_bytes()).hexdigest() == payload["source_checksums"][filename]
    doc = Document(repo_id=repo.id, source_id=uuid.uuid4(), source_type="knowledge_file",
                   title=filename, content=source.read_text(encoding="utf-8"), meta={"filename": filename})
    db.add(doc); db.flush()
    suite = load_eval_suite(db, repo.id, payload["name"])
    assert len(suite.cases) == 14 and suite.metadata["ready"]
    assert suite.metadata["review_status"] == "source_checked_not_independently_human_reviewed"
    assert all(c.reference_outcome and str(doc.id) in c.expected_source_ids for c in suite.cases)


@pytest.mark.parametrize("defect", ["no_outcome", "duplicate_id"])
def test_schema3_rejects_missing_outcome_or_duplicate_ids(db, tmp_path, defect):
    repo = Repository(owner="local", name="test", full_name="local/test")
    db.add(repo); db.flush()
    case = {"id": "one", "question": "q", "reference": "facts", "reference_outcome": "goal"}
    cases = [case]
    if defect == "no_outcome":
        case.pop("reference_outcome")
    else:
        cases.append(dict(case))
    (tmp_path / "test.json").write_text(json.dumps({"name": "test", "schema_version": 3, "cases": cases}), encoding="utf-8")
    with pytest.raises(ValueError, match="reference_outcome|重复"):
        load_eval_suite(db, repo.id, "test", suite_dir=tmp_path)


def test_workspace_tools_depend_on_checkout_but_rag_remains(db, monkeypatch, tmp_path):
    repo = Repository(owner="local", name="tools", full_name="local/tools")
    db.add(repo); db.flush()
    monkeypatch.setattr(chat, "_repo_checkout_path", lambda _: tmp_path / "missing")
    unavailable = chat._build_conversation_tools(repo, None, db)
    assert not any(name.startswith("workspace.") for name in unavailable)
    assert "rag.search_similar_documents" in unavailable
    monkeypatch.setattr(chat, "_repo_checkout_path", lambda _: tmp_path)
    available = chat._build_conversation_tools(repo, None, db)
    assert all(name in available for name in ("workspace.list_files", "workspace.read_file", "workspace.search_code"))


@pytest.mark.asyncio
async def test_overview_without_checkout_uses_indexed_evidence():
    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="根据已入库文档，后端负责生成和保存穿搭。")

    result = await ChatAgent(model=Model()).run(
        {"message": "这个项目有什么功能？"},
        {"tools": {}, "evaluation_mode": True, "evidence": [{"source_id": "doc", "source_type": "knowledge_file",
                                                            "title": "流程", "snippet": "后端生成和保存穿搭。"}]})
    assert "已入库文档" in result["answer"]
    assert not any(c["tool_name"].startswith("workspace.") for c in result["tool_calls"])
    assert result["evaluation_trace"]["retrievals"]


@pytest.mark.asyncio
async def test_reference_and_outcome_never_enter_agent_prompt(db, monkeypatch):
    monkeypatch.setattr(settings, "milvus_enabled", False)
    repo = Repository(owner="local", name="isolation", full_name="local/isolation")
    db.add(repo); db.flush()
    doc = Document(repo_id=repo.id, source_type="project_doc", source_id=uuid.uuid4(), title="facts",
                   content="已索引的项目事实", meta={})
    db.add(doc); db.commit()
    captured = []

    class Agent:
        async def run(self, input_data, context):
            captured.append(json.dumps({"input": input_data, "context": context}, default=str, ensure_ascii=False))
            return {"answer": "依据文档的回答", "tool_calls": [], "evaluation_trace": {"retrievals": [
                {"contexts": [doc.content], "sources": [{"source_id": str(doc.source_id)}]}]}}

    scorer = OfflineScorer()
    await run_rag_quality_eval(db, str(repo.id), cases=[RagEvalCase(
        question="项目事实？", reference="REFERENCE_NOT_SHARED", reference_outcome="OUTCOME_NOT_SHARED",
        expected_source_ids=[str(doc.source_id)])], scorer=scorer, agent_factory=Agent)
    assert "REFERENCE_NOT_SHARED" not in captured[0] and "OUTCOME_NOT_SHARED" not in captured[0]
    assert scorer.calls[0]["reference_outcome"] == "OUTCOME_NOT_SHARED"
