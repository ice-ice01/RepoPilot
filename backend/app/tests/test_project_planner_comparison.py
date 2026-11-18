import json

import pytest

from app.schemas.workflow import WorkflowSpec
from app.services.agents.rule_planner_agent import RulePlannerAgent as PlannerAgent
from app.services.evals.planner_comparison import CATALOG
from app.services.evals.project_planner_comparison import EvidenceClient, build_cases, digest, project_report, project_score, run_frozen


def real_context():
    return {"has_repo": True, "repo_id": "46a4bd07-41a7-4821-b3aa-ebed42e572b1",
            "has_issue": True, "issue_id": "61703b05-6756-4ef4-bdd1-6cdfc1117a5e", "issue_number": 1,
            "has_pr": True, "pr_id": "82179787-968d-466d-9cbc-1b4a3b028daf", "pr_number": 2,
            "has_ci_run": True, "has_failed_ci": False,
            "workflow_run_id": "d6c315b7-2614-4be2-80b8-d89ac22f820c", "workflow_run_number": 37002998264,
            "requires_safety": False}


def test_questions_bind_to_supplied_real_numbers_not_synthetic_suite():
    items = build_cases({"number": 1, "title": "默认形象支持删除"}, {"number": 2},
                        {"number": 36998838302}, {"number": 37002998264})
    assert len(items) == len({item.id for item in items}) == 20
    messages = " ".join(item.message for item in items)
    assert all(token in messages for token in ("Issue #1", "PR #2", "CI #36998838302", "CI #37002998264"))
    assert not any(token in messages for token in ("#142", "#86", "#9001"))
    assert all(set(item.expected) <= set(CATALOG) for item in items)


@pytest.mark.asyncio
async def test_evidence_is_passed_without_expected_labels():
    class Capture:
        usage = {}

        async def chat_json(self, prompt, payload):
            self.payload = payload
            return {"tasks": [], "reason": "test"}

    inner = Capture()
    snapshot = {"repository": {"full_name": "ice-ice01/wardrobeAgent"}, "issues": [{"number": 1}]}
    client = EvidenceClient(snapshot, inner)
    await client.chat_json("same unchanged prompt", {"message": "回顾 Issue #1", "context": real_context()})
    assert inner.payload["project_snapshot"] == snapshot
    assert "expected" not in inner.payload and "rationale" not in inner.payload
    assert "synthetic" not in json.dumps(inner.payload)


@pytest.mark.asyncio
async def test_scoring_detects_wrong_repository_target_even_when_agent_set_matches():
    context = real_context()
    item = build_cases({"number": 1, "title": "默认形象支持删除"}, {"number": 2}, {"number": 10}, {"number": 11})[0]
    spec = WorkflowSpec.model_validate(await PlannerAgent().run({"message": item.message}, context))
    assert project_score(spec, item, context)["target_binding_ok"]
    next(task for task in spec.tasks if task.task_type == "issue_analysis").claim.target_id = "another-project"
    result = project_score(spec, item, context)
    assert result["exact"]  # Selection alone is insufficient for a real project.
    assert not result["target_binding_ok"] and not result["valid_graph_and_availability"]


@pytest.mark.asyncio
async def test_scoring_detects_widened_permissions():
    context = real_context()
    item = build_cases({"number": 1, "title": "默认形象支持删除"}, {"number": 2}, {"number": 10}, {"number": 11})[0]
    spec = WorkflowSpec.model_validate(await PlannerAgent().run({"message": item.message}, context))
    spec.tasks[0].claim.allowed_sources.append("credentials")
    assert not project_score(spec, item, context)["target_binding_ok"]


def test_fixture_hash_survives_json_round_trip():
    item = build_cases({"number": 1, "title": "默认形象支持删除"}, {"number": 2}, {"number": 10}, {"number": 11})[0]
    fixture = {"case": item.__dict__, "context": real_context()}
    assert digest(fixture) == digest(json.loads(json.dumps(fixture)))


@pytest.mark.asyncio
async def test_offline_report_does_not_claim_model_success_or_write_safety():
    context = real_context()
    item = build_cases({"number": 1, "title": "默认形象支持删除"}, {"number": 2}, {"number": 10}, {"number": 11})[0]
    frozen = {"snapshot": {"repository": {"full_name": "ice-ice01/wardrobeAgent", "last_sync_at": "2026-10-06T11:33:07+00:00"},
                            "counts": {}, "issues": [], "pull_requests": [], "workflow_runs": [], "knowledge_sources": []},
              "snapshot_sha256": "test", "fixture_sha256": "test",
              "fixtures": [{"case": item.__dict__, "context": context}], "target_resolution_checks": []}
    report = await run_frozen(frozen, repeats=1, concurrency=1, live=False)
    assert report["summary"]["hybrid"] == {"runs": 0}
    assert report["rows"][0]["target_binding_ok"]
    markdown = project_report(report)
    assert "未调用真实模型" in markdown
    assert "本组无写入用例） | 未测 | 未测" in markdown
    assert "2026-10-06 19:33:07 北京时间" in markdown
