from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.workflow import WorkflowObservation, WorkflowPlanningTrace, WorkflowSpec, WorkflowTaskResult
from app.services.agents.base import BaseAgent
from app.services.agents.planner_policy import (
    PLANNER_PROMPT, TASK_CATALOG, available_tasks, compile_plan, required_tasks,
)
from app.services.agents.rule_planner_agent import RulePlannerAgent
from app.services.llm.client import LLMClient, LLMJSONError


logger = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    """工作流规划入口：模型提出任务，程序校验并补齐，失败时使用旧规则。

    阅读时可以先分清四个名字：
    - input_data：本次请求，例如 {"message": "只审查 PR #2"}。
    - context：后端准备的完整运行上下文，包含当前仓库和已选对象的 ID 等。
    - payload：从上述数据中挑出的模型输入；不会把完整 context 原样发送。
    - spec：WorkflowSpec，即程序最后交给执行器的正式任务计划。
    本类只生成计划；具体专用 Agent 由 WorkflowOrchestrator 调度执行。
    """

    name = "planner_agent"
    description = "通过模型规划并校验有边界的工程工作流，模型不可用时使用规则兜底。"
    available_tools = ["create_workflow_spec"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        # 正式运行未传 llm 时，创建真实客户端，读取项目共用的模型配置。
        # 测试可以传入假的客户端，这样能验证流程而不产生真实模型请求。
        self.llm = llm if llm is not None else LLMClient()
        self.rules = RulePlannerAgent()

    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """首次规划：准备备用计划，再尝试模型规划。"""
        # 先运行旧规则，取得用户目标、验收标准、工作流编号和备用任务。
        # 注意：不会把这份规则选出的任务发给模型，也不会与成功的模型任务取并集。
        # 模型成功时，compile_plan 会重新生成任务清单；仅失败时才使用备用任务。
        # model_validate 可以理解为“按规定格式检查数据并转成计划对象”。
        baseline = WorkflowSpec.model_validate(await self.rules.run(input_data, self._rule_context(context)))
        return await self._plan(baseline, context)

    async def replan(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """重规划：参考执行结果和 Observer 反馈，只补充必要任务。"""
        spec = WorkflowSpec.model_validate(input_data["spec"])
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        observation = WorkflowObservation.model_validate(input_data["observation"])
        pinned = dict(context)
        pinned["planner_repository_summary"] = dict(context.get("planner_repository_summary") or {})
        # claim 是任务绑定的对象和访问范围。假如先前计划审查 PR #2，
        # 后来同步出了 PR #3，已有任务仍须锁定 PR #2；不相关的新摘要也要移除。
        for task in spec.tasks:
            if task.task_type in {"issue_analysis", "pr_review", "ci_debug"}:
                _, _, key, _ = TASK_CATALOG[task.task_type]
                if context.get(key) != task.claim.target_id:
                    prefix = {"issue_analysis": "issue_", "pr_review": "pr_", "ci_debug": "ci_"}[task.task_type]
                    pinned["planner_repository_summary"] = {
                        name: value for name, value in pinned["planner_repository_summary"].items() if not name.startswith(prefix)
                    }
                pinned[key] = task.claim.target_id
                flag, number = {"issue_analysis": ("has_issue", "issue_number"),
                                "pr_review": ("has_pr", "pr_number"),
                                "ci_debug": ("has_ci_run", "workflow_run_number")}[task.task_type]
                pinned[flag] = True
                pinned[number] = task.input_hint.get(number)
        # 之前已要求人工确认，或 Observer 发现缺少安全检查时，强制保留安全门。
        pinned["requires_safety"] = bool(
            pinned.get("requires_safety") or spec.requires_human_confirmation
            or any(t.claim.write_intent for t in spec.tasks)
            or any(f.finding_type == "missing_safety_review" for f in observation.findings)
        )
        return await self._plan(spec, pinned, results=results, observation=observation, replan_input=input_data)

    async def _plan(
        self, spec: WorkflowSpec, context: dict[str, Any], *,
        results: list[WorkflowTaskResult] | None = None,
        observation: WorkflowObservation | None = None,
        replan_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """首次规划和重规划共用的四步：组装输入 → 调模型 → 校验 → 必要时兜底。"""
        replan = replan_input is not None
        # 第一步：只把这几个字段交给模型。能力的文字说明另放在 PLANNER_PROMPT 中。
        # goal=用户目标；available_tasks=可选任务；required_tasks=程序强制任务；
        # repository=已同步仓库的简短事实摘要。context 中的其他字段不自动进入请求。
        payload: dict[str, Any] = {
            "mode": "replan" if replan else "plan", "goal": spec.goal,
            "available_tasks": available_tasks(context),
            "required_tasks": required_tasks(context, spec, replan=replan),
            "repository": self._repository_summary(context),
        }
        # 同一任务可能有多条结果，以下按 task_id 保留最后一条，用于判断依赖是否成功。
        latest_results = {item.task_id: item for item in results or []}
        successful = {key for key, item in latest_results.items() if item.status == "success"}
        failed = {key for key, item in latest_results.items() if item.status in {"error", "skipped"}}
        if replan:
            # 重规划才额外发送：已有任务、结果摘要、Observer 发现的问题及建议。
            # 摘要限制长度和数量，不把完整分析报告或日志拼进模型请求。
            payload["existing_tasks"] = [
                {"id": task.id, "task_type": task.task_type, "objective": task.objective[:300],
                 "dependencies": task.dependencies} for task in spec.tasks
            ]
            payload["task_results"] = [
                {"task_id": item.task_id, "task_type": item.task_type, "status": item.status,
                 "summary": item.summary[:400], "evidence_count": item.evidence_count, "confidence": item.confidence}
                for item in list(latest_results.values())[:12]
            ]
            payload["observation"] = [
                {"finding_type": item.finding_type, "severity": item.severity,
                 "message": item.message[:300], "recommendation": item.recommendation[:300]}
                for item in (observation.findings if observation else [])[:8]
            ]
        started = time.perf_counter()
        fallback_reason = None
        guarded: list[str] = []
        try:
            # 第二步：真正调用模型。system 内容是 PLANNER_PROMPT，user 内容是 payload。
            # await 表示等待请求结果，同时允许服务处理其他异步工作。
            # 总等待时间包含连接、模型处理和解码；默认 30 秒，超时后不自动重试。
            async with asyncio.timeout(settings.planner_timeout_seconds):
                raw = await self.llm.chat_json(
                    PLANNER_PROMPT, payload, timeout_seconds=settings.planner_timeout_seconds,
                    max_tokens=settings.planner_max_tokens, raise_on_error=True,
                )
            # 第三步：模型返回的 JSON 只是候选方案，校验并绑定 Agent/目标/权限后才生效。
            # guarded 记录模型漏选、但程序强制补入的任务。
            output, guarded = compile_plan(raw, spec, context, replan=replan,
                                           successful_ids=successful, failed_ids=failed)
            reason = raw["reason"]
        except Exception as exc:
            # 第四步：缺少密钥、网络/HTTP 错误、超时、非法 JSON 或非法任务图均走旧规则。
            # 请求被主动取消时会正常终止（CancelledError 不由这里的 Exception 捕获）。
            # 只保存固定错误码，避免把供应商原文或密钥写入诊断记录。
            if isinstance(exc, LLMJSONError):
                fallback_reason = str(exc) if str(exc) in {
                    "not_configured", "timeout", "invalid_response", "connection_error",
                } or str(exc).startswith("http_") and str(exc)[5:].isdigit() else "model_error"
            elif isinstance(exc, TimeoutError):
                fallback_reason = "timeout"
            elif isinstance(exc, (ValidationError, ValueError, TypeError, KeyError)):
                fallback_reason = "invalid_plan"
            else:
                fallback_reason = "model_error"
            output = (
                WorkflowSpec.model_validate(await self.rules.replan(replan_input, self._rule_context(context)))
                if replan else spec.model_copy(deep=True)
            )
            # 旧规则重规划可能只补任务，下面同时保证人工确认标记不会丢失。
            output.requires_human_confirmation = bool(
                output.requires_human_confirmation or context.get("requires_safety")
                or any(task.claim.write_intent for task in output.tasks)
            )
            reason = "模型规划不可用或未通过校验，使用规则规划。"
            # 规则兜底也要检查任务图并补齐必选任务，不跳过最终程序检查。
            output, guarded = compile_plan(
                {"tasks": [], "reason": reason}, output, context, replan=True,
                successful_ids=successful, failed_ids=failed,
            )
            logger.info("Planner rule fallback: stage=%s code=%s", "replan" if replan else "plan", fallback_reason)
        # 每次规划留下记录：llm=采用模型方案；rule_fallback=采用规则备用方案。
        # mode 说明方案来源，不代表语义判断一定正确；仍需后续 Observer/人工审核。
        output.planning_trace.append(WorkflowPlanningTrace(
            stage="replan" if replan else "plan", mode="rule_fallback" if fallback_reason else "llm",
            reason=reason, fallback_reason=fallback_reason,
            duration_ms=int((time.perf_counter() - started) * 1000), guarded_tasks=guarded,
        ))
        # model_dump 将计划对象转换为可通过接口返回、写入 JSON 字段的数据。
        return output.model_dump(mode="json")

    @staticmethod
    def _rule_context(context: dict[str, Any]) -> dict[str, Any]:
        # 规则兜底和模型共用对象可用性判断，避免生成未绑定对象的专用任务。
        allowed = set(available_tasks(context))
        return {
            **context,
            "has_issue": "issue_analysis" in allowed,
            "has_pr": "pr_review" in allowed,
            "has_ci_run": "ci_debug" in allowed,
        }

    @staticmethod
    def _repository_summary(context: dict[str, Any]) -> dict[str, Any]:
        # 模型上下文的白名单：只挑仓库名、对象编号/显式指定标记、CI 失败标记，
        # 以及下方列出的事实摘要。不会自动带入完整聊天历史、工具、代码、日志或文档正文。
        # 注意：用户自己写在 goal 中的内容仍属于用户目标，会按原目标发送。
        summary = {"full_name": str(context.get("repo_full_name") or "")[:200]}
        for prefix, keys in (("issue", ("issue_number", "issue_explicit")),
                             ("pr", ("pr_number", "pr_explicit")),
                             ("ci", ("workflow_run_number", "workflow_run_explicit", "has_failed_ci"))):
            summary[prefix] = {key: context[key] for key in keys if isinstance(context.get(key), (str, int, bool))}
        source = context.get("planner_repository_summary")
        if isinstance(source, dict):
            # 这些事实摘要由 chat.py 从当前仓库的已同步记录生成，每项最多 240 字符。
            for key in ("issue_title", "issue_state", "pr_title", "pr_state", "pr_merged_at",
                        "ci_name", "ci_conclusion", "ci_branch", "ci_evidence_state", "last_sync_at"):
                if isinstance(source.get(key), str):
                    summary[key] = source[key][:240]
        return summary
