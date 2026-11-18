from typing import Literal

from pydantic import BaseModel, Field


class DuplicateCandidate(BaseModel):
    # Issue 重复候选：分数由 Pydantic 限制在 [0, 1]。
    issue_id: str
    title: str
    score: float = Field(ge=0, le=1)


class IssueEvidence(BaseModel):
    # Issue 证据必须带来源、标题和片段，置信度（如有）同样限制在 [0, 1]。
    source_type: str
    title: str
    snippet: str
    reference: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class IssueDrafts(BaseModel):
    # 输出给用户确认的草稿，不会因为生成草稿而直接写回 GitHub。
    clarification_comment: str | None = None
    task_breakdown: str | None = None


class IssueAnalysisOutput(BaseModel):
    """IssueAgent 的输出契约。

    ``model_validate`` 在 Agent 返回前把普通 dict 转成并校验为该模型：
    Literal 固定业务枚举，Field 约束数值范围，嵌套模型约束 evidence/drafts
    的内部结构；``model_dump`` 再把合法结果交给 API、工作流和 ChatAgent。
    """

    summary: str
    conclusion: Literal["进入开发", "等待澄清", "关闭", "拆分", "合并到已有 Issue"]
    conclusion_reason: str
    category: Literal["Bug", "Feature", "Question", "Documentation", "Refactor", "Test", "Ops"]
    priority: Literal["P0", "P1", "P2", "P3"]
    complexity: Literal["S", "M", "L", "XL"]
    suggested_owner: str
    owner_reason: str
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)
    evidence: list[IssueEvidence] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    drafts: IssueDrafts = Field(default_factory=IssueDrafts)
    confidence: float = Field(ge=0, le=1)


class PRReviewFinding(BaseModel):
    # 每条 PR 风险发现都有固定严重级别、证据和必须采取的动作。
    severity: Literal["P1", "P2", "P3"]
    title: str
    evidence: str
    required_action: str
    blocking: bool = False


class PRReviewOutput(BaseModel):
    """PRReviewAgent 的输出契约。

    除了固定审查结果的字段形状，``merge_recommendation`` 和 finding severity
    只能取声明的枚举值，``confidence`` 必须满足 0 到 1 的范围。
    """

    summary: str
    plan: list[str] = []
    executed_steps: list[str] = []
    merge_recommendation: Literal["建议合入", "修改后合入", "暂缓", "拒绝"] = "暂缓"
    recommendation_reason: str = ""
    review_findings: list[PRReviewFinding] = Field(default_factory=list)
    key_changes: list[str] = []
    risk_points: list[str] = []
    blocking_issues: list[str] = []
    review_checklist: list[str] = []
    test_suggestions: list[str] = []
    files_need_attention: list[str] = []
    review_comments: list[str] = []
    confidence: float = Field(ge=0, le=1)


class CIDebugOutput(BaseModel):
    """CIDebugAgent 的输出契约。

    ``failure_type`` 等 Literal 字段防止模型输出任意分类名称；列表、布尔值和
    置信度类型也会在 ``model_validate`` 阶段统一检查。
    """

    failure_summary: str
    failure_type: Literal["test", "build", "lint", "dependency", "permission", "environment", "unknown"]
    plan: list[str] = []
    executed_steps: list[str] = []
    first_error: str | None = None
    root_cause: str = ""
    possible_causes: list[str] = []
    fix_steps: list[str] = []
    debug_steps: list[str] = []
    related_files: list[str] = []
    is_merge_blocking: bool = True
    blocking_reason: str = ""
    confidence: float = Field(ge=0, le=1)


class SafetyOutput(BaseModel):
    requires_confirmation: bool
    risk_level: Literal["low", "medium", "high"]
    reason: str
    draft: str | None = None
