from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


WorkflowTaskStatus = Literal["pending", "running", "success", "skipped", "error"]
WorkflowFindingSeverity = Literal["info", "warning", "blocker"]


def _workflow_id() -> str:
    return f"workflow_{uuid.uuid4().hex[:12]}"


class WorkflowClaim(BaseModel):
    target_type: str
    target_id: str | None = None
    allowed_sources: list[str] = Field(default_factory=list)
    write_intent: bool = False
    reason: str = ""


class WorkflowTask(BaseModel):
    # 开发者笔记：单条任务的视图。
    # id/dependencies 决定任务图形状；task_type 决定用哪个执行器；
    # claim 声明这个 Agent 被允许触碰的数据范围（防止多个 Agent 越界互相覆盖）。
    id: str
    agent_name: str
    task_type: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    claim: WorkflowClaim
    input_hint: dict[str, Any] = Field(default_factory=dict)
    critical: bool = False


class WorkflowPlanningTrace(BaseModel):
    stage: Literal["plan", "replan"]
    mode: Literal["llm", "rule_fallback"]
    reason: str
    fallback_reason: str | None = None
    duration_ms: int = 0
    guarded_tasks: list[str] = Field(default_factory=list)


class WorkflowSpec(BaseModel):
    # 开发者笔记：WorkflowSpec 就是本次多 Agent 协作的"固定任务视图"。
    # 它是一次工作流的契约：目标(goal)、验收标准(acceptance_criteria)、
    # 任务清单(tasks)以及并行上限(max_parallel_tasks)。
    # PlannerAgent 生成它之后，执行期 Orchestrator / 各专用 Agent / Observer
    # 都只读这份契约，不再各自猜测目标，所以"任务视图"在一轮内是冻结的。
    workflow_id: str = Field(default_factory=_workflow_id)
    goal: str
    trigger_message: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    tasks: list[WorkflowTask] = Field(default_factory=list)
    max_parallel_tasks: int = Field(default=3, ge=1, le=8)
    requires_human_confirmation: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    planning_trace: list[WorkflowPlanningTrace] = Field(default_factory=list)


class WorkflowTaskResult(BaseModel):
    task_id: str
    agent_name: str
    task_type: str
    status: WorkflowTaskStatus
    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_count: int = 0
    confidence: float | None = Field(default=None, ge=0, le=1)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowFinding(BaseModel):
    finding_type: str
    severity: WorkflowFindingSeverity
    message: str
    task_ids: list[str] = Field(default_factory=list)
    recommendation: str = ""


class WorkflowObservation(BaseModel):
    observer_agent: str = "observer_agent"
    findings: list[WorkflowFinding] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0, le=1)
    human_review_required: bool = False
    summary: str = ""


class WorkflowReport(BaseModel):
    spec: WorkflowSpec
    task_results: list[WorkflowTaskResult]
    observation: WorkflowObservation
    final_answer: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    agent_events: list[dict[str, Any]] = Field(default_factory=list)
