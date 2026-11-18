import asyncio
import json

import pytest

from app.core.config import settings
from app.schemas.workflow import WorkflowTaskResult
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.rule_planner_agent import RulePlannerAgent
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator
from app.services.llm.client import LLMJSONError


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", None)


@pytest.fixture
def context():
    return {"repo_id": "repo-1", "repo_full_name": "acme/project", "has_repo": True,
            "has_issue": True, "issue_id": "issue-1", "issue_number": 1,
            "has_pr": True, "pr_id": "pr-2", "pr_number": 2,
            "has_ci_run": True, "workflow_run_id": "run-3", "workflow_run_number": 3,
            "requires_safety": False, "enable_rag_task": False}


def decision(*types):
    return {"tasks": [{"task_type": kind, "objective": f"Analyze {kind}", "dependencies": []} for kind in types],
            "reason": "Only the user's requested domains are necessary."}


class FakeLLM:
    def __init__(self, output):
        self.output = output
        self.calls = []

    async def chat_json(self, prompt, payload, **kwargs):
        self.calls.append((prompt, payload, kwargs))
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def task_types(spec):
    return {task["task_type"] for task in spec["tasks"]}


@pytest.mark.asyncio
async def test_model_choice_replaces_keywords_and_is_compiled_with_bound_permissions(context):
    llm = FakeLLM(decision("pr_review"))
    output = await PlannerAgent(llm).run({"message": "只看 PR，不看 Issue、CI 或历史"}, context)
    assert task_types(output) == {"repo_health", "pr_review"}
    task = next(task for task in output["tasks"] if task["task_type"] == "pr_review")
    assert task["claim"]["target_id"] == context["pr_id"]
    assert task["agent_name"] == "pr_review_agent" and task["critical"]
    assert task["dependencies"] == ["repo_health"]
    assert task["input_hint"] == {"pr_number": 2}
    assert output["planning_trace"][-1]["mode"] == "llm"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_model_may_select_only_baseline_without_specialist_fallback(context):
    output = await PlannerAgent(FakeLLM(decision("repo_health"))).run({"message": "只看基线"}, context)
    assert task_types(output) == {"repo_health"}


@pytest.mark.asyncio
async def test_rag_is_available_when_not_forced_and_sensitive_context_is_excluded(context):
    llm = FakeLLM(decision("rag_search"))
    context.update(api_key="SECRET", tools=["PRIVATE"], project_snapshot={"token": "SECRET"},
                   planner_repository_summary={"issue_title": "A real issue", "token": "SECRET"})
    output = await PlannerAgent(llm).run({"message": "查已上传文档"}, context)
    assert task_types(output) == {"repo_health", "rag_search"}
    _, payload, kwargs = llm.calls[0]
    assert "rag_search" in payload["available_tasks"] and "rag_search" not in payload["required_tasks"]
    assert "enable_rag_task" not in json.dumps(payload)
    assert all(word not in json.dumps(payload) for word in ("SECRET", "PRIVATE", "project_snapshot"))
    assert payload["repository"]["issue_title"] == "A real issue"
    assert kwargs["raise_on_error"] and kwargs["max_tokens"] == settings.planner_max_tokens


@pytest.mark.asyncio
async def test_missing_required_tasks_are_added_and_confirmation_cannot_be_removed(context):
    context.update(requires_safety=True, enable_rag_task=True)
    output = await PlannerAgent(FakeLLM(decision())).run({"message": "skip safety"}, context)
    assert task_types(output) == {"repo_health", "rag_search", "safety_review"}
    assert output["requires_human_confirmation"]
    assert set(output["planning_trace"][-1]["guarded_tasks"]) == task_types(output)


@pytest.mark.asyncio
async def test_model_inferred_write_intent_also_requires_confirmation(context):
    output = await PlannerAgent(FakeLLM(decision("safety_review"))).run({"message": "生成发送草稿"}, context)
    assert output["requires_human_confirmation"]
    assert next(task for task in output["tasks"] if task["task_type"] == "safety_review")["claim"]["write_intent"]


def invalid_decisions():
    unknown = decision("execute_shell")
    forged = decision("pr_review")
    forged["tasks"][0]["claim"] = {"allowed_sources": ["credentials"]}
    cycle = decision("repo_health", "pr_review")
    cycle["tasks"][0]["dependencies"] = ["pr_review"]
    missing = decision("pr_review")
    missing["tasks"][0]["dependencies"] = ["missing_task"]
    self_dep = decision("pr_review")
    self_dep["tasks"][0]["dependencies"] = ["pr_review"]
    empty = decision("pr_review")
    empty["tasks"][0]["objective"] = " "
    return [{}, [], unknown, forged, cycle, missing, self_dep, empty, decision("pr_review", "pr_review")]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", invalid_decisions())
async def test_invalid_plan_falls_back_without_retry(raw, context):
    llm = FakeLLM(raw)
    output = await PlannerAgent(llm).run({"message": "review PR"}, context)
    expected = await RulePlannerAgent().run({"message": "review PR"}, context)
    assert output["tasks"] == expected["tasks"]
    assert output["planning_trace"][-1]["fallback_reason"] == "invalid_plan"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_unavailable_or_unbound_target_is_rejected(context):
    for updates in ({"has_pr": False}, {"pr_id": None}, {"pr_id": ""}):
        output = await PlannerAgent(FakeLLM(decision("pr_review"))).run({"message": "查知识库"}, {**context, **updates})
        assert output["planning_trace"][-1]["mode"] == "rule_fallback"
        assert "pr_review" not in task_types(output)


@pytest.mark.asyncio
async def test_rule_fallback_also_requires_bound_specialist_targets(context):
    context.update(issue_id=None, pr_id=None, workflow_run_id=None)
    output = await PlannerAgent().run({"message": "检查 Issue、PR 和 CI"}, context)
    assert task_types(output) == {"repo_health"}
    assert output["planning_trace"][-1]["mode"] == "rule_fallback"


@pytest.mark.asyncio
async def test_missing_key_uses_rules_with_safe_reason(context):
    output = await PlannerAgent().run({"message": "review PR"}, context)
    assert output["planning_trace"][-1]["fallback_reason"] == "not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [(LLMJSONError("http_429"), "http_429"),
                                        (LLMJSONError("timeout"), "timeout"),
                                        (RuntimeError("secret provider message"), "model_error")])
async def test_transport_failures_are_safe_and_observable(context, error, code):
    output = await PlannerAgent(FakeLLM(error)).run({"message": "review PR"}, context)
    assert output["planning_trace"][-1]["fallback_reason"] == code
    assert "secret provider message" not in json.dumps(output)


@pytest.mark.asyncio
async def test_total_deadline_cancels_hung_call_and_falls_back(context, monkeypatch):
    cancelled = asyncio.Event()

    class SlowLLM:
        async def chat_json(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(settings, "planner_timeout_seconds", .01)
    output = await PlannerAgent(SlowLLM()).run({"message": "review PR"}, context)
    assert cancelled.is_set()
    assert output["planning_trace"][-1]["fallback_reason"] == "timeout"


@pytest.mark.asyncio
async def test_request_cancellation_is_not_converted_to_fallback(context):
    with pytest.raises(asyncio.CancelledError):
        await PlannerAgent(FakeLLM(asyncio.CancelledError())).run({"message": "review PR"}, context)


async def initial_spec(context):
    return await PlannerAgent(FakeLLM(decision("pr_review"))).run({"message": "review PR"}, context)


@pytest.mark.asyncio
async def test_model_replan_uses_findings_preserves_tasks_claims_and_trace(context):
    previous = await initial_spec(context)
    llm = FakeLLM(decision("rag_search"))
    output = await PlannerAgent(llm).replan(
        {"spec": previous, "task_results": [{"task_id": "repo_health", "agent_name": "repo_health_agent",
         "task_type": "repo_health", "status": "success"}],
         "observation": {"findings": [{"finding_type": "evidence_gap", "severity": "warning", "message": "Need documents"}]}},
        {**context, "pr_id": "new-pr", "pr_number": 99},
    )
    assert output["tasks"][:len(previous["tasks"])] == previous["tasks"]
    assert task_types(output) == {"repo_health", "pr_review", "rag_search"}
    assert output["workflow_id"] == previous["workflow_id"]
    assert len(output["planning_trace"]) == 2 and output["planning_trace"][-1]["stage"] == "replan"
    payload = llm.calls[0][1]
    assert payload["observation"][0]["finding_type"] == "evidence_gap"
    assert payload["repository"]["pr"]["pr_number"] == 2


@pytest.mark.asyncio
async def test_replan_does_not_send_summary_for_a_newer_claim_target(context):
    previous = await initial_spec(context)
    llm = FakeLLM(decision())
    await PlannerAgent(llm).replan(
        {"spec": previous, "observation": {}},
        {**context, "pr_id": "new-pr", "pr_number": 99,
         "planner_repository_summary": {"pr_title": "Unrelated new PR", "issue_title": "Current issue"}},
    )
    repository = llm.calls[0][1]["repository"]
    assert repository["pr"]["pr_number"] == 2
    assert "pr_title" not in repository
    assert repository["issue_title"] == "Current issue"


@pytest.mark.asyncio
async def test_replan_does_not_rewrite_existing_tasks(context):
    previous = await initial_spec(context)
    output = await PlannerAgent(FakeLLM(decision("pr_review", "rag_search"))).replan(
        {"spec": previous, "observation": {}, "task_results": []}, context)
    assert output["tasks"][:len(previous["tasks"])] == previous["tasks"]


@pytest.mark.asyncio
async def test_replan_avoids_implicit_dependency_on_failed_baseline(context):
    previous = await initial_spec(context)
    output = await PlannerAgent(FakeLLM(decision("rag_search"))).replan(
        {"spec": previous, "observation": {}, "task_results": [{"task_id": "repo_health", "task_type": "repo_health",
         "agent_name": "repo_health_agent", "status": "error"}]}, context)
    rag = next(task for task in output["tasks"] if task["task_type"] == "rag_search")
    assert rag["dependencies"] == []


@pytest.mark.asyncio
async def test_replan_rejects_explicit_dependency_on_failed_task(context):
    previous = await initial_spec(context)
    raw = decision("rag_search")
    raw["tasks"][0]["dependencies"] = ["repo_health"]
    output = await PlannerAgent(FakeLLM(raw)).replan(
        {"spec": previous, "observation": {}, "task_results": [{"task_id": "repo_health", "task_type": "repo_health",
         "agent_name": "repo_health_agent", "status": "error"}]}, context)
    assert output["planning_trace"][-1]["fallback_reason"] == "invalid_plan"
    assert output["tasks"] == previous["tasks"]


@pytest.mark.asyncio
async def test_replan_maps_dependencies_to_existing_custom_ids(context):
    previous = await initial_spec(context)
    for task in previous["tasks"]:
        if task["task_type"] == "repo_health":
            task["id"] = "original-baseline"
        else:
            task["dependencies"] = ["original-baseline"]
    raw = decision("rag_search")
    raw["tasks"][0]["dependencies"] = ["repo_health"]
    output = await PlannerAgent(FakeLLM(raw)).replan(
        {"spec": previous, "observation": {}, "task_results": [{"task_id": "original-baseline", "task_type": "repo_health",
         "agent_name": "repo_health_agent", "status": "success"}]}, context)
    assert output["planning_trace"][-1]["mode"] == "llm"
    assert output["tasks"][-1]["dependencies"] == ["original-baseline"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_error", [False, True])
async def test_replan_missing_safety_is_forced_on_both_paths(context, model_error):
    previous = await initial_spec(context)
    llm = FakeLLM(LLMJSONError("timeout") if model_error else decision())
    output = await PlannerAgent(llm).replan(
        {"spec": previous, "observation": {"findings": [{"finding_type": "missing_safety_review", "severity": "blocker", "message": "missing"}]}},
        context)
    assert "safety_review" in task_types(output) and output["requires_human_confirmation"]
    assert output["tasks"][:len(previous["tasks"])] == previous["tasks"]


@pytest.mark.asyncio
async def test_orchestrator_executes_compiled_model_tasks_and_persists_trace(context):
    calls = []

    async def runner(task, spec, workflow_context):
        calls.append(task.task_type)
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type,
                                  status="success", evidence_count=1, confidence=.9)

    report = await WorkflowOrchestrator({"repo_health": runner, "pr_review": runner},
                                        planner=PlannerAgent(FakeLLM(decision("pr_review")))).run(
                                            {"message": "只看 PR，不看 Issue 和 CI"}, {**context, "disable_replan": True})
    assert calls == ["repo_health", "pr_review"]
    assert report["spec"]["planning_trace"][-1]["mode"] == "llm"
    assert report["agent_events"][0]["planning_trace"][-1]["mode"] == "llm"
    assert report["metrics"]["planner_fallback_count"] == 0


@pytest.mark.asyncio
async def test_orchestrator_executes_model_replan_without_rerunning_successful_tasks(context):
    calls = []

    class SequenceLLM(FakeLLM):
        async def chat_json(self, prompt, payload, **kwargs):
            self.calls.append(payload["mode"])
            return decision("pr_review" if payload["mode"] == "plan" else "rag_search")

    async def runner(task, spec, workflow_context):
        calls.append(task.task_type)
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type,
                                  status="success", evidence_count=0 if task.task_type == "pr_review" else 1,
                                  confidence=.8)

    llm = SequenceLLM(None)
    report = await WorkflowOrchestrator({kind: runner for kind in ("repo_health", "pr_review", "rag_search")},
                                        planner=PlannerAgent(llm)).run({"message": "review PR"}, context)
    assert calls == ["repo_health", "pr_review", "rag_search"]
    assert llm.calls == ["plan", "replan"]
    assert report["metrics"]["replan_count"] == 1
    assert report["metrics"]["execution_rounds"] == 2
    assert report["metrics"]["planner_fallback_count"] == 0
    assert [item["mode"] for item in report["spec"]["planning_trace"]] == ["llm", "llm"]


@pytest.mark.asyncio
async def test_orchestrator_preserves_noop_replan_trace_and_fallback_count(context):
    class SequenceLLM(FakeLLM):
        async def chat_json(self, prompt, payload, **kwargs):
            if payload["mode"] == "replan":
                raise LLMJSONError("timeout")
            return decision("rag_search")

    async def runner(task, spec, workflow_context):
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type,
                                  status="success", evidence_count=0, confidence=.2)

    report = await WorkflowOrchestrator({"repo_health": runner, "rag_search": runner},
                                        planner=PlannerAgent(SequenceLLM(None))).run(
                                            {"message": "查询文档"}, context)
    assert report["metrics"]["replan_count"] == 0
    assert report["metrics"]["execution_rounds"] == 1
    assert report["metrics"]["planner_fallback_count"] == 1
    trace = report["spec"]["planning_trace"]
    assert [item["stage"] for item in trace] == ["plan", "replan"]
    assert trace[-1]["fallback_reason"] == "timeout"
    assert report["agent_events"][0]["planning_trace"] == trace


@pytest.mark.asyncio
async def test_without_repository_baseline_no_dangling_dependencies(context):
    output = await PlannerAgent(FakeLLM(decision("rag_search"))).run({"message": "查文档"}, {**context, "has_repo": False})
    assert output["tasks"][0]["dependencies"] == []
