import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.services.agents.base import BaseAgent
from app.services.context_compression import (
    calculate_context_pressure,
    clip_to_token_budget,
    default_context_budget,
    estimate_tokens,
    is_context_overflow_error,
    prompt_cache_model_kwargs,
)
from app.services.skills import SkillRegistry, SkillRegistryError


ConversationTool = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
ToolErrorKind = Literal[
    "unknown_tool",
    "timeout",
    "rate_limited",
    "transient_network",
    "permission_denied",
    "data_not_found",
    "model_output_invalid",
    "tool_runtime_error",
]


class GenericToolArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: str | None = Field(default=None, description="原始用户消息")
    query: str | None = Field(default=None, description="搜索查询")
    keyword: str | None = Field(default=None, description="关键词过滤")
    limit: int | None = Field(default=None, description="结果数量上限")
    repo_id: str | None = Field(default=None, description="仓库 UUID")
    repo_full_name: str | None = Field(default=None, description="仓库完整名称")
    conversation_id: str | None = Field(default=None, description="会话 UUID")
    session_id: str | None = Field(default=None, description="会话片段 UUID")
    message_id: str | None = Field(default=None, description="需要精确读取的消息 UUID")
    before_message_id: str | None = Field(default=None, description="只读取该消息之前的记录")
    after_message_id: str | None = Field(default=None, description="只读取该消息之后的记录")
    offset: int | None = Field(default=None, description="从消息正文的字符偏移量开始读取")
    max_chars: int | None = Field(default=None, description="本次最多返回的正文字符数")
    include_transcript: bool | None = Field(default=None, description="读取封存会话时是否包含原始 transcript")
    route: str | None = Field(default=None, description="仓库路由或意图")
    path: str | None = Field(default=None, description="相对工作区的文件或目录路径")
    pattern: str | None = Field(default=None, description="文本或 glob 模式")
    start_line: int | None = Field(default=None, description="读取文件时从第几行开始，行号从 1 开始")
    line_count: int | None = Field(default=None, description="最多读取多少行")


class ConversationState(TypedDict, total=False):
    # TypedDict 描述运行状态字典的字段；total=False 表示字段可以暂时不存在。
    # context 是服务端组装的输入；observations 是工具结果摘要；agent_events 是执行轨迹。
    message: str
    context: dict[str, Any]
    tools: dict[str, StructuredTool]
    tool_name_map: dict[str, str]
    observations: list[dict[str, Any]]
    agent_events: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    stream_events: list[dict[str, Any]]
    route: str
    step: int
    final_answer: str
    stop_reason: str
    completion_status: str
    tool_call_counts: dict[str, int]
    evaluation_trace: dict[str, Any]
    model_usage: list[dict[str, Any]]
    prompt_diagnostics: list[dict[str, Any]]
    runtime_context_stats: dict[str, Any]
    context_overflow_recovery_attempted: bool


class ChatAgent(BaseAgent):
    name = "chat_agent"
    description = "使用原生工具调用的对话 Agent。LLM 选择 LangChain 工具，记忆工具通过 MCP 执行。"
    available_tools = [
        "devflow_search_evidence",
        "devflow_get_thread_context",
        "devflow_read_session_events",
        "workspace.list_files",
        "workspace.read_file",
        "workspace.search_code",
        "repo_health.generate_summary",
        "rag.search_similar_documents",
        "issue_agent.analyze_issue",
        "pr_review_agent.analyze_pr",
        "ci_debug_agent.analyze_workflow_run",
        "report_agent.generate_weekly_report",
        "safety_agent.classify_action_risk",
        "workflow.run_engineering_review",
    ]

    max_steps = 12
    max_repeated_tool_calls = 2
    tool_retry_attempts = 1

    # 初始化模型：优先使用传入的模型，否则在已配置 API Key 时创建 ChatOpenAI 客户端。
    def __init__(self, model: ChatOpenAI | None = None) -> None:
        self.model = model
        if self.model is None and settings.llm_api_key:
            self.model = ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                temperature=0,
                timeout=60,
                model_kwargs=prompt_cache_model_kwargs(),
            )

    # 执行一轮对话，收集流式事件并返回最终结果，缺少最终事件时返回默认结果。
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        final: dict[str, Any] | None = None
        async for event in self.run_stream(input_data, context):
            if event["event"] == "final":
                final = event["data"]
        return final or {
            "run_id": "",
            "route": "unknown",
            "tool_calls": [],
            "answer": "没有生成最终回答。",
            "citations": [],
        }

    # 以流式方式执行对话，处理技能激活、上下文预取和工具循环，并输出过程事件及最终回答。
    async def run_stream(self, input_data: dict[str, Any], context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        message = str(input_data.get("message") or "")
        runtime_context = dict(context)
        try:
            active_skills = self._activate_skills(message, runtime_context)
        except SkillRegistryError as exc:
            answer = f"Skill 激活失败：{exc}"
            async for chunk in self._stream_answer(answer):
                yield chunk
            yield self._event(
                "final",
                {
                    "run_id": "",
                    "route": "skill_error",
                    "tool_calls": [],
                    "agent_events": [{"type": "skill_activation_error", "error": str(exc)}],
                    "answer": answer,
                    "citations": [],
                    "completion": {"status": "blocked", "reason": "skill_activation_error", "steps": 0, "tool_call_count": 0, "tool_error_count": 0},
                },
            )
            return
        runtime_context["active_skills"] = active_skills
        raw_tools: dict[str, ConversationTool] = runtime_context["tools"]
        tools = self._build_langchain_tools(raw_tools, runtime_context)
        state: ConversationState = {
            # 本轮状态从空工具轨迹开始；历史记录通过 runtime_context 注入，不直接全量加载。
            "message": message,
            "context": runtime_context,
            "tools": tools,
            "tool_name_map": self._tool_name_map(raw_tools),
            "observations": [],
            "agent_events": [],
            "tool_calls": [],
            "citations": [],
            "pending_tool_calls": [],
            "stream_events": [],
            "route": "direct_answer",
            "step": 0,
            "final_answer": "",
            "stop_reason": "",
            "completion_status": "running",
            "tool_call_counts": {},
            "model_usage": [],
            "prompt_diagnostics": [],
            "runtime_context_stats": {},
            "context_overflow_recovery_attempted": False,
        }
        if context.get("evaluation_mode"):
            state["evaluation_trace"] = self._initial_evaluation_trace(message, context)
        self._append_agent_event(
            state,
            "agent_loop_start",
            {
                "mode": "langgraph_tool_calling_loop",
                "max_steps": self.max_steps,
                "available_tools": list(self._tool_name_map(raw_tools).values()),
            },
        )
        for skill in active_skills:
            activation_event = {
                key: value
                for key, value in skill.items()
                if key
                in {
                    "skill_name",
                    "skill_title",
                    "skill_version",
                    "entrypoint",
                    "tools",
                    "workflow_steps",
                    "output_contract",
                    "safety_level",
                    "activation_mode",
                    "activation_reason",
                    "instruction_digest",
                }
            }
            activation_event["resources"] = [
                {key: value for key, value in resource.items() if key != "content"}
                for resource in skill.get("resources") or []
                if isinstance(resource, dict)
            ]
            self._append_agent_event(
                state,
                "skill_activated",
                activation_event,
            )
        prefetched_evidence = context.get("evidence") if isinstance(context.get("evidence"), list) else []
        pre_retrieval_enabled = "evidence" in context
        reasoning_retrieval_enabled = "rag.search_similar_documents" in raw_tools
        pre_retrieval_sources = [
            {
                "source_id": str(item.get("source_id") or item.get("id") or ""),
                "source_type": str(item.get("source_type") or "evidence"),
                "title": str(item.get("title") or "未命名证据"),
                "score": item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            }
            for item in prefetched_evidence[:8]
            if isinstance(item, dict)
        ]
        self._append_agent_event(
            state,
            "rag_strategy",
            {
                "mode": "hybrid_rag" if pre_retrieval_enabled and reasoning_retrieval_enabled else "tool_calling",
                "pre_retrieval": {
                    "enabled": pre_retrieval_enabled,
                    "evidence_count": len(prefetched_evidence),
                    "query": message,
                    "sources": pre_retrieval_sources,
                },
                "reasoning_retrieval": {
                    "enabled": reasoning_retrieval_enabled,
                    "supports_query_rewrite": reasoning_retrieval_enabled,
                    "supports_multiple_searches": reasoning_retrieval_enabled,
                },
            },
        )

        memory_policy = self._memory_context_policy(message, context)
        if memory_policy:
            self._append_agent_event(state, "context_policy", memory_policy)
        if memory_policy and memory_policy.get("mode") == "direct_recent_history":
            state["final_answer"] = await self._direct_answer_from_recent_history(state)
            answer = str(state.get("final_answer") or "").strip()
            if not answer:
                answer = self._local_recent_history_answer(state)
            self._stop_loop(state, "recent_history_available", "completed")
            self._record_skill_validation(state)
            async for chunk in self._stream_answer(answer):
                yield chunk
            yield self._event(
                "final",
                {
                    "run_id": "",
                    "route": "direct_answer",
                    "tool_calls": [],
                    "agent_events": state.get("agent_events") or [],
                    "answer": answer,
                    "citations": [],
                    "completion": self._completion_summary(state),
                    **self._runtime_metadata(state),
                    **self._evaluation_result(state),
                },
            )
            return

        required_memory_tool = str((memory_policy or {}).get("tool") or "") or None
        if required_memory_tool and required_memory_tool in raw_tools:
            async for event in self._prefetch_memory_context(state, required_memory_tool):
                yield event
            state["final_answer"] = await self._final_from_observations(state)
            answer = str(state.get("final_answer") or "").strip()
            if not answer:
                answer = await self._fallback_answer(state)
            self._stop_loop(state, "required_memory_context_loaded", self._tool_completion_status(state))
            self._record_skill_validation(state)
            async for chunk in self._stream_answer(answer):
                yield chunk
            yield self._event(
                "final",
                {
                    "run_id": "",
                    "route": state.get("route") or required_memory_tool,
                    "tool_calls": state.get("tool_calls") or [],
                    "agent_events": state.get("agent_events") or [],
                    "answer": answer,
                    "citations": state.get("citations") or [],
                    "completion": self._completion_summary(state),
                    **self._runtime_metadata(state),
                    **self._evaluation_result(state),
                },
            )
            return

        if self._asks_project_overview(message) and "workspace.list_files" in raw_tools:
            async for event in self._prefetch_project_context(state):
                yield event
            state["final_answer"] = await self._final_from_observations(state)
            answer = str(state.get("final_answer") or "").strip()
            if not answer:
                answer = await self._fallback_answer(state)
            self._stop_loop(state, "project_context_loaded", self._tool_completion_status(state))
            self._record_skill_validation(state)
            async for chunk in self._stream_answer(answer):
                yield chunk
            yield self._event(
                "final",
                {
                    "run_id": "",
                    "route": state.get("route") or "workspace_read_file",
                    "tool_calls": state.get("tool_calls") or [],
                    "agent_events": state.get("agent_events") or [],
                    "answer": answer,
                    "citations": state.get("citations") or [],
                    "completion": self._completion_summary(state),
                    **self._runtime_metadata(state),
                    **self._evaluation_result(state),
                },
            )
            return

        if self.model is None:
            answer = self._local_prefetched_evidence_answer(state) or (
                "当前没有取得足够证据。请先确认仓库已连接并完成同步；开放式对话还需要配置 LLM。"
            )
            evidence = (state.get("context") or {}).get("evidence")
            has_evidence = isinstance(evidence, list) and any(isinstance(item, dict) for item in evidence)
            self._stop_loop(
                state,
                "prefetched_evidence_available" if has_evidence else "llm_not_configured",
                "completed" if has_evidence else "blocked",
            )
            self._record_skill_validation(state)
            async for chunk in self._stream_answer(answer):
                yield chunk
            yield self._event(
                "final",
                {
                    "run_id": "",
                    "route": "prefetched_evidence" if has_evidence else "direct_answer",
                    "tool_calls": [],
                    "agent_events": state.get("agent_events") or [],
                    "answer": answer,
                    "citations": self._prefetched_evidence_citations(state),
                    "completion": self._completion_summary(state),
                    **self._runtime_metadata(state),
                    **self._evaluation_result(state),
                },
            )
            return

        graph = self._build_agent_graph(self._tool_model(tools))
        async for event in self._stream_agent_graph(graph, state):
            yield event

        answer = str(state.get("final_answer") or "").strip()
        if not answer:
            answer = await self._fallback_answer(state)
        if state.get("completion_status") == "running":
            self._stop_loop(state, "fallback_answer_ready", self._tool_completion_status(state))
        self._record_skill_validation(state)
        async for chunk in self._stream_answer(answer):
            yield chunk
        yield self._event(
            "final",
            {
                "run_id": "",
                "route": state.get("route") or "direct_answer",
                "tool_calls": state.get("tool_calls") or [],
                "agent_events": state.get("agent_events") or [],
                "answer": answer,
                "citations": state.get("citations") or [],
                "completion": self._completion_summary(state),
                **self._runtime_metadata(state),
                **self._evaluation_result(state),
            },
        )

    # 构建并编译对话状态图，连接模型决策、工具执行和步数上限处理节点。
    def _build_agent_graph(self, tool_model: Any):
        # 将绑定工具的模型传入决策方法，作为状态图的异步决策节点。
        async def model_decision_node(state: ConversationState) -> dict[str, Any]:
            return await self._graph_model_decision(state, tool_model)

        graph = StateGraph(ConversationState)
        graph.add_node("model_decision", model_decision_node)
        graph.add_node("tool_action", self._graph_tool_action)
        graph.add_node("max_steps_stop", self._graph_max_steps_stop)
        graph.add_edge(START, "model_decision")
        graph.add_conditional_edges(
            "model_decision",
            self._route_after_model_decision,
            {"tools": "tool_action", "end": END},
        )
        graph.add_conditional_edges(
            "tool_action",
            self._route_after_tool_action,
            {"tools": "tool_action", "model": "model_decision", "limit": "max_steps_stop"},
        )
        graph.add_edge("max_steps_stop", END)
        return graph.compile(name="chat_agent_tool_calling_graph")

    # 运行状态图，同步节点更新，并将工具调用预告和节点事件逐个输出。
    async def _stream_agent_graph(self, graph: Any, state: ConversationState) -> AsyncIterator[dict[str, Any]]:
        current_state: ConversationState = dict(state)
        config = {"recursion_limit": self.max_steps * 8 + 20}
        async for graph_event in graph.astream(current_state, config=config, stream_mode="debug"):
            if not isinstance(graph_event, dict):
                continue
            payload = graph_event.get("payload") if isinstance(graph_event.get("payload"), dict) else {}
            node_name = str(payload.get("name") or "")
            if graph_event.get("type") == "task" and node_name == "tool_action":
                preview = self._preview_tool_use_event(payload.get("input"))
                if preview:
                    yield self._event("tool_use", preview)
                continue
            if graph_event.get("type") != "task_result":
                continue
            result = payload.get("result")
            if not isinstance(result, dict):
                continue
            for key, value in result.items():
                if key != "stream_events":
                    current_state[key] = value
                    state[key] = value
            for event in result.get("stream_events") or []:
                if isinstance(event, dict):
                    yield event

    # 调用模型决定使用工具还是直接回答，并处理上下文溢出重试和重复调用拦截。
    async def _graph_model_decision(self, state: ConversationState, tool_model: Any) -> dict[str, Any]:
        # LangGraph 每一步：模型判断 -> 工具执行 -> 把结果放进下一步提示词 -> 再判断。
        # 历史/观察被序列化进 HumanMessage，不是把数据库消息逐条发成原生消息轮次。
        next_state: ConversationState = dict(state)
        step = int(next_state.get("step") or 0) + 1
        next_state["step"] = step
        self._append_agent_event(next_state, "agent_step", {"step": step, "phase": "model_decision"})
        system_prompt = self._tool_calling_system_prompt()
        user_prompt = self._tool_calling_user_prompt(next_state)
        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        estimated_prompt_tokens = self._record_prompt_diagnostics(
            next_state,
            phase="model_decision",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        try:
            response = await tool_model.ainvoke(messages)
        except Exception as exc:
            # 只对上下文超长错误重试；标志保存在本轮状态中，整轮最多恢复一次。
            # 仅缩小失败调用的提示词，不重新执行已经完成的工具，也不写数据库摘要边界。
            if not is_context_overflow_error(exc) or next_state.get("context_overflow_recovery_attempted"):
                raise
            next_state["context_overflow_recovery_attempted"] = True
            next_state["runtime_context_stats"] = {
                **(next_state.get("runtime_context_stats") or {}),
                "reactive_compaction": "aggressive_retry",
                "overflow_error": str(exc),
            }
            self._append_agent_event(
                next_state,
                "context_overflow_recovery",
                {"phase": "model_decision", "strategy": "aggressive_runtime_projection"},
            )
            user_prompt = self._tool_calling_user_prompt(next_state, aggressive=True)
            messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            estimated_prompt_tokens = self._record_prompt_diagnostics(
                next_state,
                phase="model_decision_reactive_retry",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            response = await tool_model.ainvoke(messages)
        self._record_model_usage(
            next_state,
            response,
            phase="model_decision",
            estimated_prompt_tokens=estimated_prompt_tokens,
        )
        response_text = self._message_content(response).strip()
        raw_tool_calls = getattr(response, "tool_calls", None) or []
        tool_calls = [dict(call) for call in raw_tool_calls if isinstance(call, dict)]
        stream_events: list[dict[str, Any]] = []
        if response_text and tool_calls:
            self._append_agent_event(next_state, "thought", {"content": response_text})
            stream_events.append(self._event("thought", {"content": response_text}))
        if not tool_calls:
            next_state["final_answer"] = response_text
            next_state["pending_tool_calls"] = []
            self._stop_loop(next_state, "model_final_answer", "completed")
        else:
            repeated = self._repeated_tool_call(tool_calls, next_state)
            if repeated:
                next_state["pending_tool_calls"] = []
                next_state["route"] = "tool_loop_guard"
                next_state["final_answer"] = (
                    "我停止了这轮 Agent Loop，因为模型连续请求相同工具和参数，"
                    "继续执行只会重复消耗预算。请根据已有工具结果调整问题，或让我换一种检索/分析路径。"
                )
                next_state["observations"] = [
                    *(next_state.get("observations") or []),
                    {
                        "tool_name": repeated["tool_name"],
                        "arguments": repeated["arguments"],
                        "observation": {
                            "route": "tool_loop_guard",
                            "answer_preview": "Detected repeated identical tool call and stopped the loop.",
                            "repeat_count": repeated["repeat_count"],
                            "fingerprint": repeated["fingerprint"],
                        },
                    },
                ]
                self._stop_loop(
                    next_state,
                    "repeated_tool_call",
                    "blocked",
                    {
                        "tool_name": repeated["tool_name"],
                        "arguments": repeated["arguments"],
                        "repeat_count": repeated["repeat_count"],
                    },
                )
                return self._graph_state_update(next_state, stream_events)
            next_state["pending_tool_calls"] = tool_calls
        return self._graph_state_update(next_state, stream_events)

# 根据大模型选出的工具名称，找到对应python函数并执行
    # 执行待处理队列中的一个工具调用，并生成结果、动作和观察事件。
    async def _graph_tool_action(self, state: ConversationState) -> dict[str, Any]:
        next_state: ConversationState = dict(state)
        pending = list(next_state.get("pending_tool_calls") or [])
        if not pending:
            return self._graph_state_update(next_state, [])
        tool_call = pending[0]
        next_state["pending_tool_calls"] = pending[1:]
        public_action, action_input, observation, _tool_message, _tool_use_event, tool_result_event = await self._execute_tool_call(tool_call, next_state)
        stream_events = [
            self._event("tool_result", tool_result_event),
            self._event("action", {"tool_name": public_action, "arguments": action_input}),
            self._event("observation", {"tool_name": public_action, "content": observation}),
        ]
        return self._graph_state_update(next_state, stream_events)

    # 在达到最大决策步数时结束循环，设置预算耗尽状态和提示回答。
    async def _graph_max_steps_stop(self, state: ConversationState) -> dict[str, Any]:
        next_state: ConversationState = dict(state)
        next_state["route"] = "tool_loop_budget_exhausted"
        next_state["final_answer"] = (
            "这轮 Agent Loop 已达到步骤预算上限。下面的结论只能基于已经完成的工具调用和观察结果，"
            "如果还需要继续，我会从当前证据继续缩小范围。"
        )
        self._stop_loop(next_state, "max_steps_exhausted", "budget_exhausted", {"max_steps": self.max_steps})
        return self._graph_state_update(next_state, [])

    # 根据模型是否提出工具调用，决定转入工具节点还是结束对话。
    def _route_after_model_decision(self, state: ConversationState) -> str:
        return "tools" if state.get("pending_tool_calls") else "end"

    # 根据待执行工具和剩余步数，决定继续执行工具、返回模型或触发预算停止。
    def _route_after_tool_action(self, state: ConversationState) -> str:
        if state.get("pending_tool_calls"):
            return "tools"
        if int(state.get("step") or 0) >= self.max_steps and not str(state.get("final_answer") or "").strip():
            return "limit"
        return "model"

    # 提取需要回写状态图的字段，并附上本次节点产生的流式事件。
    def _graph_state_update(self, state: ConversationState, stream_events: list[dict[str, Any]]) -> dict[str, Any]:
        keys = [
            "observations",
            "agent_events",
            "tool_calls",
            "citations",
            "pending_tool_calls",
            "route",
            "step",
            "final_answer",
            "stop_reason",
            "completion_status",
            "tool_call_counts",
            "evaluation_trace",
            "model_usage",
            "prompt_diagnostics",
            "runtime_context_stats",
            "context_overflow_recovery_attempted",
        ]
        update = {key: state[key] for key in keys if key in state}
        update["stream_events"] = stream_events
        return update

    # 设置循环停止原因和完成状态，并记录停止事件。
    def _stop_loop(
        self,
        state: ConversationState,
        reason: str,
        completion_status: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state["stop_reason"] = reason
        state["completion_status"] = completion_status
        return self._append_agent_event(
            state,
            "agent_loop_stop",
            {"reason": reason, "completion_status": completion_status, **(extra or {})},
        )

    # 累计相同工具及参数的调用次数，返回超过重复次数限制的调用信息。
    def _repeated_tool_call(self, tool_calls: list[dict[str, Any]], state: ConversationState) -> dict[str, Any] | None:
        counts = dict(state.get("tool_call_counts") or {})
        for tool_call in tool_calls:
            safe_action, public_action, _tool_use_id, action_input = self._tool_call_parts(tool_call, state)
            fingerprint = self._tool_call_fingerprint(safe_action, action_input)
            repeat_count = counts.get(fingerprint, 0) + 1
            counts[fingerprint] = repeat_count
            if repeat_count > self.max_repeated_tool_calls:
                state["tool_call_counts"] = counts
                return {
                    "tool_name": public_action,
                    "arguments": action_input,
                    "fingerprint": fingerprint,
                    "repeat_count": repeat_count,
                }
        state["tool_call_counts"] = counts
        return None

    # 将工具名称和排序后的参数序列化为指纹，用于识别重复调用。
    def _tool_call_fingerprint(self, safe_action: str, action_input: dict[str, Any]) -> str:
        return f"{safe_action}:{json.dumps(action_input, ensure_ascii=False, sort_keys=True, default=str)}"

    # 汇总对话完成状态、停止原因、决策步数及工具调用和错误数量。
    def _completion_summary(self, state: ConversationState) -> dict[str, Any]:
        tool_calls = [call for call in state.get("tool_calls") or [] if isinstance(call, dict)]
        error_calls = [call for call in tool_calls if call.get("status") == "error"]
        status = state.get("completion_status") or "completed"
        if status == "completed" and error_calls:
            status = "completed_with_tool_errors"
        return {
            "status": status,
            "reason": state.get("stop_reason") or "model_final_answer",
            "steps": int(state.get("step") or 0),
            "tool_call_count": len(tool_calls),
            "tool_error_count": len(error_calls),
        }

    # 根据工具执行结果判断任务是否被阻塞，全部调用失败时返回阻塞状态。
    def _tool_completion_status(self, state: ConversationState) -> str:
        calls = [call for call in state.get("tool_calls") or [] if isinstance(call, dict)]
        if calls and not any(call.get("status") == "success" for call in calls):
            return "blocked"
        return "completed"

    # 从待执行队列提取首个工具的信息，生成执行前展示的调用预告。
    def _preview_tool_use_event(self, raw_state: Any) -> dict[str, Any] | None:
        if not isinstance(raw_state, dict):
            return None
        pending = raw_state.get("pending_tool_calls")
        if not isinstance(pending, list) or not pending or not isinstance(pending[0], dict):
            return None
        safe_action, public_action, tool_use_id, action_input = self._tool_call_parts(pending[0], raw_state)
        return {
            "type": "tool_use",
            "step": raw_state.get("step", 0),
            "id": tool_use_id,
            "tool_name": public_action,
            "arguments": action_input,
            **self._skill_event_metadata(public_action, raw_state),
        }

    # 执行一次工具调用，并按顺序流式输出调用、结果、动作和观察事件。
    async def _execute_tool_call_events(
        self,
        tool_call: dict[str, Any],
        state: ConversationState,
    ) -> AsyncIterator[dict[str, Any]]:
        safe_action, public_action, tool_use_id, action_input, tool_use_event = self._prepare_tool_call(tool_call, state)
        yield self._event("tool_use", tool_use_event)
        observation, _tool_message, tool_result_event = await self._complete_tool_call(
            safe_action,
            public_action,
            tool_use_id,
            action_input,
            state,
        )
        yield self._event("tool_result", tool_result_event)
        yield self._event("action", {"tool_name": public_action, "arguments": action_input})
        yield self._event("observation", {"tool_name": public_action, "content": observation})

    # 执行一次工具调用，返回工具名称、参数、观察摘要、工具消息和调用事件。
    async def _execute_tool_call(
        self,
        tool_call: dict[str, Any],
        state: ConversationState,
    ) -> tuple[str, dict[str, Any], dict[str, Any], ToolMessage, dict[str, Any], dict[str, Any]]:
        safe_action, public_action, tool_use_id, action_input, tool_use_event = self._prepare_tool_call(tool_call, state)
        observation, tool_message, tool_result_event = await self._complete_tool_call(
            safe_action,
            public_action,
            tool_use_id,
            action_input,
            state,
        )
        return public_action, action_input, observation, tool_message, tool_use_event, tool_result_event

    # 解析工具调用并记录调用事件，为实际执行准备参数和标识。
    def _prepare_tool_call(
        self,
        tool_call: dict[str, Any],
        state: ConversationState,
    ) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
        safe_action, public_action, tool_use_id, action_input = self._tool_call_parts(tool_call, state)
        tool_use_event = self._append_agent_event(
            state,
            "tool_use",
            {"id": tool_use_id, "tool_name": public_action, "arguments": action_input, **self._skill_event_metadata(public_action, state)},
        )
        return safe_action, public_action, tool_use_id, action_input, tool_use_event

    # 解析工具名称、调用标识和参数，并补齐当前上下文要求的参数。
    def _tool_call_parts(
        self,
        tool_call: dict[str, Any],
        state: ConversationState | dict[str, Any],
    ) -> tuple[str, str, str, dict[str, Any]]:
        safe_action = str(tool_call.get("name") or "").strip()
        public_action = self._public_tool_name(safe_action, state)  # type: ignore[arg-type]
        tool_use_id = str(tool_call.get("id") or f"tool_use_{len(state.get('agent_events') or []) + 1}")
        raw_args = tool_call.get("args") if isinstance(tool_call, dict) else {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                raw_args = {}
        action_input = self._normalize_action_input(public_action, raw_args if isinstance(raw_args, dict) else {}, state)  # type: ignore[arg-type]
        return safe_action, public_action, tool_use_id, action_input

    # 检查工具权限并执行调用，汇总观察、引用和轨迹，生成工具结果消息。
    async def _complete_tool_call(
        self,
        safe_action: str,
        public_action: str,
        tool_use_id: str,
        action_input: dict[str, Any],
        state: ConversationState,
    ) -> tuple[dict[str, Any], ToolMessage, dict[str, Any]]:
        tools = state.get("tools") or {}
        skill_violation = self._explicit_skill_tool_violation(public_action, state)
        if skill_violation:
            error = skill_violation
            error_kind: ToolErrorKind = "permission_denied"
            observation = {
                "route": "tool_error",
                "answer": error,
                "citations": [],
                "tool_calls": [
                    {
                        "tool_name": public_action,
                        "arguments": action_input,
                        "status": "error",
                        "error": error,
                        "error_kind": error_kind,
                        "retryable": False,
                        "attempts": 1,
                        "tool_use_id": tool_use_id,
                    }
                ],
                "error_kind": error_kind,
                "retryable": False,
            }
        elif safe_action not in tools:
            error = f"未知工具：{public_action}。可用工具：{', '.join(state.get('tool_name_map', {}).values())}"
            error_kind: ToolErrorKind = "unknown_tool"
            observation = {
                "route": "tool_error",
                "answer": error,
                "citations": [],
                "tool_calls": [
                    {
                        "tool_name": public_action,
                        "arguments": action_input,
                        "status": "error",
                        "error": error,
                        "error_kind": error_kind,
                        "retryable": False,
                        "attempts": 1,
                        "tool_use_id": tool_use_id,
                    }
                ],
                "error_kind": error_kind,
                "retryable": False,
            }
        else:
            observation = await self._invoke_tool_with_recovery(
                tools[safe_action],
                public_action,
                tool_use_id,
                action_input,
                state,
            )

        if not observation.get("tool_calls"):
            observation["tool_calls"] = [
                {
                    "tool_name": public_action,
                    "arguments": action_input,
                    "status": "success",
                    "tool_use_id": tool_use_id,
                    **self._skill_event_metadata(public_action, state),
                }
            ]
        for call in observation.get("tool_calls", []):
            if isinstance(call, dict):
                call.setdefault("tool_use_id", tool_use_id)
                if observation.get("attempts") is not None:
                    call.setdefault("attempts", observation.get("attempts"))
                if observation.get("error_kind") is not None:
                    call.setdefault("error_kind", observation.get("error_kind"))
                if observation.get("retryable") is not None:
                    call.setdefault("retryable", observation.get("retryable"))
                call.update(self._skill_event_metadata(str(call.get("tool_name") or public_action), state))
        self._capture_evaluation_observation(
            state,
            tool_name=public_action,
            arguments=action_input,
            observation=observation,
        )
        # 先做“单条工具结果裁剪”，再把摘要保存到 observations 和 tool_result 事件。
        # 后续总预算投影不会再改这些已记录摘要，但超长原始工具正文在此可能已经被丢弃。
        summary = self._summarize_observation(observation)
        status = "error" if any(call.get("status") == "error" for call in observation.get("tool_calls", []) if isinstance(call, dict)) else "success"
        tool_result_event = self._append_agent_event(
            state,
            "tool_result",
            {
                "tool_use_id": tool_use_id,
                "tool_name": public_action,
                "status": status,
                "route": observation.get("route"),
                "error_kind": observation.get("error_kind"),
                "retryable": observation.get("retryable"),
                "content": summary,
                **self._skill_event_metadata(public_action, state),
            },
        )
        state["observations"] = [
            *(state.get("observations") or []),
            {"tool_name": public_action, "arguments": action_input, "observation": summary},
        ]
        state["tool_calls"] = [*(state.get("tool_calls") or []), *observation.get("tool_calls", [])]
        state["citations"] = [*(state.get("citations") or []), *observation.get("citations", [])]
        extra_events = observation.get("agent_events")
        if isinstance(extra_events, list):
            state["agent_events"] = [*(state.get("agent_events") or []), *[event for event in extra_events if isinstance(event, dict)]]
        observation_route = str(observation.get("route") or "")
        has_successful_tool = any(call.get("status") == "success" for call in state.get("tool_calls") or [])
        if observation_route and (observation_route != "tool_error" or not has_successful_tool):
            state["route"] = observation_route

        tool_message = ToolMessage(
            content=json.dumps(summary, ensure_ascii=False),
            tool_call_id=tool_use_id,
        )
        return summary, tool_message, tool_result_event

    # 调用工具，对允许重试的临时错误进行有限重试，并统一返回成功或错误结果。
    async def _invoke_tool_with_recovery(
        self,
        tool: StructuredTool,
        public_action: str,
        tool_use_id: str,
        action_input: dict[str, Any],
        state: ConversationState,
    ) -> dict[str, Any]:
        max_attempts = 1 + self.tool_retry_attempts
        last_error: Exception | None = None
        last_error_kind: ToolErrorKind = "tool_runtime_error"
        retryable = False
        for attempt in range(1, max_attempts + 1):
            try:
                observation = await tool.ainvoke(action_input)
                if isinstance(observation, dict):
                    observation.setdefault("attempts", attempt)
                    return observation
                return {
                    "route": public_action,
                    "answer": str(observation),
                    "citations": [],
                    "tool_calls": [],
                    "attempts": attempt,
                }
            except Exception as exc:
                last_error = exc
                last_error_kind = self._classify_tool_error(exc)
                retryable = self._is_retriable_tool_error(last_error_kind, public_action)
                if not retryable or attempt >= max_attempts:
                    break
                self._append_agent_event(
                    state,
                    "tool_retry",
                    {
                        "tool_use_id": tool_use_id,
                        "tool_name": public_action,
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "error_kind": last_error_kind,
                        "error": str(exc),
                    },
                )
        error_text = str(last_error or "unknown tool failure")
        return {
            "route": "tool_error",
            "answer": f"工具 {public_action} 执行失败：{error_text}",
            "citations": [],
            "tool_calls": [
                {
                    "tool_name": public_action,
                    "arguments": action_input,
                    "status": "error",
                    "error": error_text,
                    "error_kind": last_error_kind,
                    "retryable": retryable,
                    "attempts": max_attempts if retryable else 1,
                    "tool_use_id": tool_use_id,
                }
            ],
            "error_kind": last_error_kind,
            "retryable": retryable,
            "attempts": max_attempts if retryable else 1,
        }

    # 根据异常类型和错误文本，将工具失败归类为超时、限流、权限等错误。
    def _classify_tool_error(self, exc: Exception) -> ToolErrorKind:
        if isinstance(exc, TimeoutError):
            return "timeout"
        text = str(exc).lower()
        if any(token in text for token in ["timeout", "timed out", "deadline"]):
            return "timeout"
        if any(token in text for token in ["rate limit", "429", "too many requests"]):
            return "rate_limited"
        if any(token in text for token in ["connection", "temporarily", "unavailable", "reset by peer", "503", "502", "504"]):
            return "transient_network"
        if any(token in text for token in ["permission", "forbidden", "unauthorized", "401", "403"]):
            return "permission_denied"
        if any(token in text for token in ["not found", "missing", "404"]):
            return "data_not_found"
        if any(token in text for token in ["validation", "schema", "json"]):
            return "model_output_invalid"
        return "tool_runtime_error"

    # 结合错误类别与工具类型，判断失败后是否允许自动重试。
    def _is_retriable_tool_error(self, error_kind: ToolErrorKind, public_action: str) -> bool:
        if error_kind not in {"timeout", "rate_limited", "transient_network"}:
            return False
        if public_action in {"safety_agent.classify_action_risk", "report_agent.generate_weekly_report", "workflow.run_engineering_review"}:
            return False
        return True

    # 将可用工具绑定到语言模型，模型未配置时抛出异常。
    def _tool_model(self, tools: dict[str, StructuredTool]):
        if self.model is None:
            raise RuntimeError("ChatAgent requires an LLM for open-ended tool selection")
        return self.model.bind_tools(list(tools.values()))

    # 生成工具调用模式的系统提示词，规定任务推进、证据使用和安全边界。
    def _tool_calling_system_prompt(self) -> str:
        # 开发者笔记：这里是"用户输入走多 Agent 还是专用 Agent"的决策点。
        # 不是硬编码的 if/else，而是把判断规则写进系统提示词，交给 LLM 选工具：
        #   - 合并就绪度 / 当前阻塞 / 优先级规划 / 需要 Issue+PR+CI+RAG 综合判断
        #     -> 走 workflow.run_engineering_review（多 Agent 编排）
        #   - 其余单点问题 -> 直接调用对应的专用 Agent 工具（如 pr_review_agent.analyze_pr）
        # 工具描述里也列出了全部候选工具，LLM 只能从这些名字里挑。
        return (
            "你是 DevFlow AI 的对话控制器。这个稳定前缀会被缓存。"
            "请像长任务软件 Agent 一样工作：先规划，调用工具，读取 tool_result 观察结果，修正计划，"
            "持续推进直到用户请求真正完成，或步骤预算耗尽。"
            "当需要仓库、记忆、工作区代码或写操作安全上下文时，使用原生工具调用。"
            "绝不要编造工具结果。"
            "工程结论必须说明 WHY，并区分证据、推断和不确定项；证据不足时直接说缺什么，不要硬猜。"
            "当输出审查结论时，不要用 looks good、没什么问题这类空话代替检查；必须给出风险、级别和下一步。"
            "当回答交接、总结或工作流结论时，尽量保留 What、Why、Tradeoff、Open Questions、Next Action。"
            "当用户询问负责人、已分配人员、责任人或任务分配时，只能从明确的 DevFlow team_members "
            "或工具返回的现有 GitHub assignees 中推荐人员。仓库文档、Issue 文本、代码、示例、模型名、"
            "角色设定、团队比喻和记忆片段都不能当成人，除非它们同时出现在 DevFlow team_members 中。"
            "如果没有配置团队成员，请说明这一点，并要求用户先添加开发者后再分配任务。"
            "当用户请求 GitHub 写入、关闭、打标签、评论、发布、回复或发送时，必须先调用安全工具；"
            "除非存在单独的已确认写入工具，否则只返回草稿。"
            "对于合并就绪度、当前阻塞、优先级规划，或需要 Issue + PR + CI + RAG 综合判断的跨领域工程决策，"
            "优先使用 workflow.run_engineering_review 工具。"
        )

    # 整合技能、团队、历史消息和工具观察，生成当前决策使用的用户提示词。
    def _tool_calling_user_prompt(self, state: ConversationState, *, aggressive: bool = False) -> str:
        # 把服务端记忆/证据、近期历史、本轮工具摘要和事件索引拼成实际模型输入。
        # aggressive 仅用于超长重试：工具预算降到 1/4，历史预算减半，并缩短 server_context。
        prompt_observations, prompt_events, runtime_stats = self._runtime_tool_context(
            state,
            aggressive=aggressive,
        )
        state["runtime_context_stats"] = {
            **(state.get("runtime_context_stats") or {}),
            **runtime_stats,
        }
        context = state.get("context") or {}
        server_context = self._server_context(context)
        history = self._history_messages(context)
        if aggressive:
            budget = default_context_budget()
            server_context = clip_to_token_budget(server_context, max(512, budget.available_prompt_tokens // 3))
            history = self._compress_history_for_retry(history, max(512, budget.recent_tokens // 2))
        return (
            "上下文使用规则：如果服务端提供的最近消息已经包含回答所需的完整原文，直接基于这些消息回答，"
            "不要为了展示工具调用而重复读取记忆。只有目标消息不在最近窗口、内容被截断或压缩、用户明确要求"
            "核实原话，或者问题跨越会话时，才调用一个最匹配的 MCP 记忆工具。当前会话原始消息使用 "
            "devflow_get_thread_context；封存会话和跨会话摘要使用 devflow_read_session_events；项目长期记忆、"
            "历史决策和仓库证据使用 devflow_search_evidence。询问当前会话原话时，不要额外调用 "
            "devflow_search_evidence。\n\n"
            "来源规则：工作区工具可用时，涉及源码行为、依赖、架构或项目概览的问题须先检查 README、package 或相关源码。"
            "如果可用工具列表中没有 workspace 工具，说明当前未提供本地源码访问能力，不要尝试调用或声称读过源码。"
            "此时可依据已入库文档和上传知识回答文档问题；需要源码才能核实的部分应说明尚无法核实，并提示同步仓库。"
            "文档问答优先使用已有相关证据，不要仅为展示工具调用读取源码目录。\n\n"
            "涉及源码的问题，优先使用 workspace.search_code、workspace.read_file 或 workspace.list_files。"
            "涉及已索引的仓库历史和上传知识，使用 rag.search_similar_documents 或 devflow_search_evidence。\n\n"
            "混合 RAG 约定：服务端上下文中的 [Related Evidence] 是根据原始问题完成的调用前检索。"
            "如果这些证据不足、互相冲突，或问题包含多个子问题，请改写为更具体的 query 调用 "
            "rag.search_similar_documents；可以用不同 query 连续补查，并在最终回答前比较多次 tool_result。\n\n"
            "工作流硬性规则：如果用户请求多 Agent 审查、合并就绪度、当前阻塞、工程状态、跨 Agent 综合，"
            "或依赖 Issue、PR、CI 与仓库证据共同判断的决策，必须先调用 workflow.run_engineering_review，"
            "再给出最终建议。\n\n"
            "分配硬性规则：如果用户询问谁应该负责、处理、接手或被分配某个 Issue/PR/任务，"
            "可选人员只能来自下方列出的 DevFlow team_members，以及工具明确返回的现有 GitHub assignees。"
            "不要把仓库内容、项目角色设定、模型名、示例、作者或虚构标签当成团队成员。"
            "如果 team_members 为空且没有现有 assignee，请回答当前还没有添加项目成员，"
            "并要求用户在团队面板中添加开发者。可以说明可能需要的能力方向，但不要点名具体人员。\n\n"
            "循环约定：每次工具调用都会先记录为 tool_use，再记录为 tool_result。决定下一步前必须阅读之前的 tool_result 内容。"
            "如果目录列表里出现值得继续查看的 README、manifest、代码文件、Issue、PR 或 CI 记录，不要停在目录列表。"
            "除非新证据使重复调用有必要，不要用相同参数重复调用同一个工具。\n\n"
            f"当前 Agent 配置的运行模型：{settings.llm_model}\n\n"
            f"可用原生工具：\n{self._tool_help(state)}\n\n"
            f"已注册的 DevFlow 技能：\n{self._registered_skills_context(state.get('context') or {})}\n\n"
            f"本轮按需加载的 Skill 完整指令：\n{self._active_skill_prompt(state.get('context') or {})}\n\n"
            f"DevFlow 团队上下文：\n{self._team_assignment_context(state.get('context') or {})}\n\n"
            f"服务端上下文：\n{server_context}\n\n"
            f"上下文覆盖度：\n{json.dumps(self._history_coverage(context), ensure_ascii=False)}\n\n"
            f"最近消息：\n{json.dumps(history, ensure_ascii=False)}\n\n"
            f"已有观察结果（已按总预算投影，完整结果保存在会话转录）：\n{json.dumps(prompt_observations, ensure_ascii=False)}\n\n"
            f"工具调用/结果索引：\n{json.dumps(prompt_events, ensure_ascii=False)}\n\n"
            f"用户消息：{state.get('message') or ''}"
        )

    # 按工具类型补齐消息、查询和分页默认值，并注入服务端仓库及会话标识。
    def _normalize_action_input(self, action: str, action_input: dict[str, Any], state: ConversationState) -> dict[str, Any]:
        normalized = dict(action_input)
        message = str(state.get("message") or "")
        context = state.get("context") or {}

        if action in {
            "repo_health.generate_summary",
            "rag.search_similar_documents",
            "issue_agent.analyze_issue",
            "pr_review_agent.analyze_pr",
            "ci_debug_agent.analyze_workflow_run",
            "report_agent.generate_weekly_report",
            "safety_agent.classify_action_risk",
            "workflow.run_engineering_review",
            "workspace.search_code",
        }:
            normalized.setdefault("message", message)
        if action == "workspace.search_code":
            normalized.setdefault("query", message)
        if action == "rag.search_similar_documents":
            normalized.setdefault("query", str(normalized.get("message") or message))
        if action == "workspace.read_file" and not normalized.get("path") and self._asks_project_overview(message):
            normalized["path"] = "README.md"
        if action == "devflow_search_evidence":
            normalized.setdefault("query", message)
        if action == "devflow_get_thread_context":
            normalized.setdefault("limit", 20)
        if action == "devflow_read_session_events":
            normalized.setdefault("limit", 5)
        if action.startswith("devflow_") and context.get("repo_id"):
            normalized["repo_id"] = context["repo_id"]
            normalized.pop("repo_full_name", None)
        if action.startswith("devflow_") and context.get("conversation_id"):
            normalized["conversation_id"] = context["conversation_id"]
        return normalized

    # 通过关键词判断用户是否在询问项目概览、架构、技术栈或启动方式。
    def _asks_project_overview(self, message: str) -> bool:
        lowered = message.lower()
        if any(token in lowered for token in ["issue", "pull request", "pr #", "ci", "未关闭", "待处理"]):
            return False
        return any(
            token in lowered
            for token in [
                "what is this project",
                "what does this project",
                "project about",
                "repository about",
                "tech stack",
                "architecture",
                "setup",
                "项目介绍",
                "项目概览",
                "这个项目",
                "该项目",
                "这个仓库",
                "该仓库",
                "\u505a\u4ec0\u4e48",
                "\u67b6\u6784",
                "\u6280\u672f\u6808",
            ]
        )

    # 根据记忆读取策略，返回当前问题必须调用的记忆工具名称。
    def _required_memory_tool(self, message: str, context: dict[str, Any] | None = None) -> str | None:
        policy = self._memory_context_policy(message, context or {})
        return str((policy or {}).get("tool") or "") or None

    # 分析历史回忆和核实需求，决定直接使用近期消息还是调用对应记忆工具。
    def _memory_context_policy(self, message: str, context: dict[str, Any]) -> dict[str, Any] | None:
        # 先决定是否需要读取记忆：近期原文足够则直接回答；逐字核实必须读数据库原文。
        # 项目历史找证据；封存轮次查 session；聊天旧消息查 thread_context。
        lowered = message.lower()
        for tool_name in [
            "devflow_search_evidence",
            "devflow_get_thread_context",
            "devflow_read_session_events",
        ]:
            if tool_name in lowered:
                return {"mode": "required_tool", "tool": tool_name, "reason": "explicit_tool_request"}
        if any(token in lowered for token in ["历史证据", "历史决策", "项目记忆", "historical evidence", "past decision"]):
            return {"mode": "required_tool", "tool": "devflow_search_evidence", "reason": "project_history_request"}
        if any(
            token in lowered
            for token in [
                "封存会话",
                "会话摘要",
                "上次会话",
                "上个会话",
                "跨会话",
                "session digest",
                "session events",
                "previous session",
                "previous conversation",
            ]
        ):
            return {"mode": "required_tool", "tool": "devflow_read_session_events", "reason": "cross_session_request"}

        immediate_recall = any(
            token in lowered
            for token in [
                "刚刚说",
                "刚才说",
                "上一条消息",
                "上一句话",
                "上一句",
                "what did i just say",
                "my last message",
            ]
        )
        broad_recall = immediate_recall or any(
            token in lowered
            for token in [
                "之前说过",
                "上次讨论",
                "聊天历史",
                "会话历史",
                "你记得",
                "前面说过",
                "what did we say",
                "earlier in this chat",
            ]
        )
        if not broad_recall:
            return None

        requires_exact_verification = any(
            token in lowered
            for token in ["原话", "逐字", "准确核实", "精确核实", "审计", "verbatim", "exact quote", "verify the record"]
        )
        if requires_exact_verification:
            return {"mode": "required_tool", "tool": "devflow_get_thread_context", "reason": "exact_verification_requested"}

        if self._recent_history_can_answer(message, context, immediate=immediate_recall):
            return {
                "mode": "direct_recent_history",
                "tool": None,
                "reason": "exact_recent_messages_available",
            }
        return {"mode": "required_tool", "tool": "devflow_get_thread_context", "reason": "recent_history_incomplete"}

    # 结合历史消息和完整性信息，判断近期原始消息是否足以回答回忆问题。
    def _recent_history_can_answer(self, message: str, context: dict[str, Any], *, immediate: bool) -> bool:
        history = self._history_messages(context)
        prior_user_index = self._last_prior_user_index(history, message)
        if prior_user_index is None:
            return False
        coverage = self._history_coverage(context)
        if not coverage:
            return True
        if immediate:
            exact_suffix = int(coverage.get("exact_suffix_messages") or 0)
            distance_from_end = len(history) - prior_user_index
            return exact_suffix >= distance_from_end
        return bool(coverage.get("complete_and_exact"))

    # 排除与本轮问题重复的末条用户消息，定位上一条用户消息的索引。
    def _last_prior_user_index(self, history: list[dict[str, str]], message: str) -> int | None:
        normalized_current = self._normalize_history_text(message)
        indexes = [index for index, item in enumerate(history) if item.get("role") == "user"]
        if indexes and self._normalize_history_text(history[indexes[-1]].get("content") or "") == normalized_current:
            indexes.pop()
        return indexes[-1] if indexes else None

    # 统一文本大小写和空白字符，便于比较历史消息是否与当前问题相同。
    def _normalize_history_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    # 让模型直接依据近期原始消息回答，模型不可用或调用失败时使用本地回答。
    async def _direct_answer_from_recent_history(self, state: ConversationState) -> str:
        if self.model is None:
            return self._local_recent_history_answer(state)
        history = self._history_messages(state.get("context") or {})
        try:
            system_prompt = (
                "请直接根据提供的近期原始消息回答，不要声称调用了任何工具。"
                "最后一条与当前问题相同的 user 消息是本轮问题，不要把它当成用户要回忆的上一条内容。"
                "只回答上下文能证明的内容；如果用户问上一条消息，优先简洁复述上一条 user 消息。"
            )
            user_prompt = (
                f"近期原始消息：{json.dumps(history, ensure_ascii=False)}\n"
                f"当前用户问题：{state.get('message') or ''}"
            )
            estimated = self._record_prompt_diagnostics(
                state,
                phase="direct_recent_history",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            # 模型invoke
            response = await self.model.ainvoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            )
            self._record_model_usage(
                state,
                response,
                phase="direct_recent_history",
                estimated_prompt_tokens=estimated,
            )
            return self._message_content(response).strip() or self._local_recent_history_answer(state)
        except Exception:
            return self._local_recent_history_answer(state)

    # 直接复述上一条用户消息，缺少可确认的记录时返回提示。
    def _local_recent_history_answer(self, state: ConversationState) -> str:
        history = self._history_messages(state.get("context") or {})
        prior_user_index = self._last_prior_user_index(history, str(state.get("message") or ""))
        if prior_user_index is None:
            return "当前上下文里没有可确认的上一条用户消息。"
        content = history[prior_user_index].get("content") or ""
        return f"你刚刚说的是：{content}"

    # 从上下文压缩统计中读取历史消息的完整性和原文覆盖信息。
    def _history_coverage(self, context: dict[str, Any]) -> dict[str, Any]:
        compression_stats = context.get("compression_stats") or {}
        if not isinstance(compression_stats, dict):
            return {}
        coverage = compression_stats.get("history_coverage") or {}
        return coverage if isinstance(coverage, dict) else {}

    # 调用指定记忆工具预取相关历史，并流式输出工具执行事件。
    async def _prefetch_memory_context(self, state: ConversationState, public_name: str) -> AsyncIterator[dict[str, Any]]:
        tools = state.get("tools") or {}
        tool_names = state.get("tool_name_map") or {}
        safe_name = next((safe for safe, public in tool_names.items() if public == public_name), None)
        if not safe_name or safe_name not in tools:
            return
        message = str(state.get("message") or "")
        args: dict[str, Any]
        if public_name == "devflow_search_evidence":
            args = {"query": message, "limit": 5}
        elif public_name == "devflow_get_thread_context":
            args = {"keyword": "", "limit": 20}
        else:
            args = {"limit": 5}
        async for event in self._execute_tool_call_events(
            {"name": safe_name, "args": args, "id": f"required_{public_name}_{len(state.get('agent_events') or [])}"},
            state,
        ):
            yield event

    # 预先列出项目文件，并读取目录结果中存在的常见说明和配置文件。
    async def _prefetch_project_context(self, state: ConversationState) -> AsyncIterator[dict[str, Any]]:
        tools = state.get("tools") or {}
        tool_names = state.get("tool_name_map") or {}
        safe_by_public = {public: safe for safe, public in tool_names.items()}

        # 通过公开工具名查找可用工具，并转发该预取调用产生的流式事件。
        async def call(public_name: str, args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
            safe_name = safe_by_public.get(public_name)
            if not safe_name or safe_name not in tools:
                return
            async for event in self._execute_tool_call_events(
                {"name": safe_name, "args": args, "id": f"prefetch_{public_name}_{len(state.get('agent_events') or [])}"},
                state,
            ):
                yield event

        async for event in call("workspace.list_files", {"path": ".", "limit": 120}):
            yield event
        observed_paths: set[str] = set()
        for item in state.get("observations") or []:
            if not isinstance(item, dict) or not isinstance(item.get("observation"), dict):
                continue
            preview = str(item["observation"].get("answer_preview") or "")
            observed_paths.update(token.strip("'\"`[](),").lower() for token in preview.split())
        for path in ["README.md", "README.zh-CN.md", "package.json", "SETUP.md"]:
            if path.lower() in observed_paths:
                async for event in call("workspace.read_file", {"path": path, "line_count": 220}):
                    yield event

    # 把原始异步工具封装为带有参数模型、描述和运行上下文的 LangChain 工具。
    def _build_langchain_tools(self, raw_tools: dict[str, ConversationTool], context: dict[str, Any]) -> dict[str, StructuredTool]:
        # ChatAgent 只负责把路由层提供的普通异步函数包装成 LangChain Tool。
        # 因此模型选择的是 issue_agent.analyze_issue 等“外层工具”，工具内部
        # 才会实例化并调用 IssueAgent/PRReviewAgent/CIDebugAgent。
        wrapped: dict[str, StructuredTool] = {}
        for name, func in raw_tools.items():
            # 绑定当前原始工具，过滤空参数后携带运行上下文执行异步调用。
            async def call_tool(_func: ConversationTool = func, **kwargs: Any) -> dict[str, Any]:
                input_data = {key: value for key, value in kwargs.items() if value is not None}
                return await _func(input_data, context)

            safe_name = self._safe_tool_name(name)
            call_tool.__name__ = safe_name
            wrapped[safe_name] = StructuredTool.from_function(
                coroutine=call_tool,
                name=safe_name,
                description=self._tool_description(name, context),
                args_schema=GenericToolArgs,
                infer_schema=False,
            )
        return wrapped

    # 建立模型可用的安全工具名到原始公开工具名的映射。
    def _tool_name_map(self, raw_tools: dict[str, ConversationTool]) -> dict[str, str]:
        return {self._safe_tool_name(name): name for name in raw_tools}

    # 替换工具名称中的不支持字符，生成适合模型工具调用的名称。
    def _safe_tool_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "__", name)

    # 根据名称映射还原公开工具名，找不到映射时保留原名称。
    def _public_tool_name(self, safe_name: str, state: ConversationState) -> str:
        return (state.get("tool_name_map") or {}).get(safe_name, safe_name)

    # 生成工具用途说明，并附加上下文提供的技能相关描述。
    def _tool_description(self, name: str, context: dict[str, Any] | None = None) -> str:
        descriptions = {
            "devflow_search_evidence": "MCP 记忆工具。搜索持久记忆、会话摘要、仓库证据、Issue、PR、CI 和 Agent 运行记录。",
            "devflow_get_thread_context": "MCP 记忆工具。跨压缩边界读取当前仓库会话的完整原始聊天转录，支持消息 UUID 和字符偏移分页。",
            "devflow_read_session_events": "MCP 记忆工具。读取已封存的会话摘要、原始消息和工具事件。",
            "workspace.list_files": "列出所选仓库本地 checkout 下的文件。只读，用于探索源码树。",
            "workspace.read_file": "从所选仓库 checkout 中读取文本文件，可指定 start_line 和 line_count。只读。",
            "workspace.search_code": "在所选仓库 checkout 中搜索代码或文档文本。只读。",
            "repo_health.generate_summary": "总结当前仓库健康状态。",
            "rag.search_similar_documents": "搜索项目历史与非结构化知识，包括 Issue 描述、PR 说明与 Review 讨论、失败 CI 日志、项目文档、上传知识和已批准的长期记忆。可传入改写后的 query，并用不同 query 连续调用以补查和交叉验证。源码优先使用 workspace 工具，团队成员和实时状态使用对应结构化工具。",
            "issue_agent.analyze_issue": "分析最新同步的 Issue。",
            "pr_review_agent.analyze_pr": "分析最新同步的 PR。",
            "ci_debug_agent.analyze_workflow_run": "分析最新失败的 CI 运行。",
            "report_agent.generate_weekly_report": "生成仓库报告。",
            "safety_agent.classify_action_risk": "安全起草有风险的写操作或外部发送动作。",
            "workflow.run_engineering_review": "运行 Planner -> 专用 Agent -> Observer -> Synthesis 工作流，用于 PR 就绪度、阻塞项、优先级和多 Agent 仓库审查等跨领域工程决策。",
        }
        suffixes = {}
        if context:
            raw_suffixes = context.get("skill_description_by_tool") or {}
            if isinstance(raw_suffixes, dict):
                suffixes = raw_suffixes
        return f"公开工具：{name}。{descriptions.get(name, 'DevFlow 工具。')}{suffixes.get(name, '')}"

    # 将当前可用工具的名称和描述整理为提示词中的帮助列表。
    def _tool_help(self, state: ConversationState) -> str:
        tools = state.get("tools") or {}
        return "\n".join(f"- {name}: {tool.description}" for name, tool in tools.items())

    # 让模型基于现有证据和工具观察生成最终回答，不可用时使用本地汇总。
    async def _final_from_observations(self, state: ConversationState) -> str:
        if self.model is None:
            return self._local_observation_answer(state)
        try:
            system_prompt = (
                "请使用用户的语言回答，并且只能基于观察结果、tool_result 记录和上下文。"
                "关键判断要说明 WHY；把证据、推断、不确定项和下一步区分开。"
                "如果是审查或合并建议，不能用 looks good、没什么问题一笔带过，必须保留风险和阻塞项。"
                "对于负责人、已分配人员、责任人或任务分配问题，只能从明确的 DevFlow team_members "
                "或工具返回的现有 GitHub assignees 中推荐人员。仓库文档、Issue 文本、代码、示例、"
                "角色设定、模型名和团队比喻都不能当成人。如果没有配置团队成员，也没有观察到现有 assignee，"
                "请说明当前还没有添加项目成员，并要求用户在团队面板中添加开发者。"
            )
            prompt_observations, prompt_events, runtime_stats = self._runtime_tool_context(state)
            state["runtime_context_stats"] = {**(state.get("runtime_context_stats") or {}), **runtime_stats}
            user_prompt = (
                f"用户消息：{state.get('message') or ''}\n"
                f"当前 Agent 配置的运行模型：{settings.llm_model}\n"
                f"DevFlow 团队上下文：{self._team_assignment_context(state.get('context') or {})}\n"
                f"上下文：{self._server_context(state.get('context') or {})}\n"
                f"观察结果：{json.dumps(prompt_observations, ensure_ascii=False)}\n"
                f"工具调用/结果索引：{json.dumps(prompt_events, ensure_ascii=False)}"
            )
            estimated = self._record_prompt_diagnostics(
                state,
                phase="final_from_observations",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            response = await self.model.ainvoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            )
            self._record_model_usage(
                state,
                response,
                phase="final_from_observations",
                estimated_prompt_tokens=estimated,
            )
            return self._message_content(response)
        except Exception:
            return self._local_observation_answer(state)

    # 在本地汇总有效工具观察，缺少观察时依次尝试预取证据和服务端上下文。
    def _local_observation_answer(self, state: ConversationState) -> str:
        observations = state.get("observations") or []
        excerpts: list[str] = []
        for item in observations:
            if not isinstance(item, dict):
                continue
            value = item.get("answer") or item.get("content") or item.get("result") or item.get("message")
            if not value and isinstance(item.get("observation"), dict):
                nested = item["observation"]
                if nested.get("route") == "tool_error":
                    continue
                value = nested.get("answer_preview") or nested.get("answer") or nested.get("content")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            text = " ".join(str(value or "").split())
            if text:
                excerpts.append(text[:600])
        if not excerpts:
            evidence_answer = self._local_prefetched_evidence_answer(state)
            if evidence_answer:
                return evidence_answer
            context_text = " ".join(self._server_context(state.get("context") or {}).split())
            if context_text:
                return "已读取当前仓库上下文。\n\n" + context_text[:1200]
            return "当前没有取得足够证据。请先确认仓库已连接并完成同步；开放式对话还需要配置 LLM。"
        return "已读取当前仓库证据。\n\n" + "\n\n".join(
            f"{index + 1}. {excerpt}" for index, excerpt in enumerate(excerpts[:4])
        )

    # 根据预取证据直接组织回答，并对未关闭 Issue 查询生成专门的列表。
    def _local_prefetched_evidence_answer(self, state: ConversationState) -> str:
        context = state.get("context") or {}
        raw_evidence = context.get("evidence")
        if not isinstance(raw_evidence, list):
            return ""
        evidence = [item for item in raw_evidence if isinstance(item, dict)]
        if not evidence:
            return ""

        message = str(state.get("message") or "").lower()
        asks_open_issues = "issue" in message and any(
            token in message for token in ["未关闭", "待处理", "open", "开放"]
        )
        if asks_open_issues:
            open_issues = [
                item
                for item in evidence
                if str(item.get("source_type") or "") == "issue"
                and str((item.get("metadata") or {}).get("state") or "").lower() == "open"
            ]
            if open_issues:
                lines = ["根据本轮预检索证据，当前仓库有这些未关闭的 Issue："]
                for item in open_issues[:8]:
                    title = str(item.get("title") or "未命名 Issue").strip()
                    snippet = " ".join(str(item.get("snippet") or "").split())
                    lines.append(f"- {title}" + (f"：{snippet[:240]}" if snippet else ""))
                lines.append("\n数据来自当前仓库已同步的 Issue 证据；本轮没有调用工作区源码工具。")
                return "\n".join(lines)

        rendered = [self._render_evaluation_context(item) for item in evidence[:8]]
        rendered = [item for item in rendered if item]
        if not rendered:
            return ""
        return "根据当前仓库已同步的证据：\n\n" + "\n\n".join(
            f"{index + 1}. {item}" for index, item in enumerate(rendered)
        )

    # 从预取证据中提取来源类型、标识和标题，生成引用列表。
    def _prefetched_evidence_citations(self, state: ConversationState) -> list[dict[str, str]]:
        context = state.get("context") or {}
        evidence = context.get("evidence")
        if not isinstance(evidence, list):
            return []
        return [
            {
                "type": str(item.get("source_type") or "evidence"),
                "id": str(item.get("source_id") or item.get("id") or ""),
                "title": str(item.get("title") or "未命名证据"),
            }
            for item in evidence[:8]
            if isinstance(item, dict)
        ]

    # 为事件附加类型和当前步数，将其追加到执行轨迹并返回。
    def _append_agent_event(self, state: ConversationState, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {
            "type": event_type,
            "step": state.get("step", 0),
            **data,
        }
        state["agent_events"] = [*(state.get("agent_events") or []), event]
        return event

    # 筛选工具调用、结果、重试、决策步骤和停止事件，形成执行记录。
    def _tool_event_transcript(self, state: ConversationState) -> list[dict[str, Any]]:
        return [
            event
            for event in state.get("agent_events") or []
            if event.get("type") in {"tool_use", "tool_result", "tool_retry", "agent_step", "agent_loop_stop"}
        ]

    # 将完整工具观察和事件整理为受 token 预算限制的提示词视图，并返回压缩统计。
    def _runtime_tool_context(
        self,
        state: ConversationState,
        *,
        aggressive: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Project complete tool state into a bounded prompt-only view.

        The full observations and events remain in ``state`` and are sealed in
        ChatSession.  Only this derived view is shortened for the next model
        call, matching Claude Code's separation between durable transcript and
        active context.
        """
        # 上面的 complete/full 指 state 中已经记录的工具摘要，不保证是工具原始大正文。
        # 这里再次按总预算取新结果优先的副本；state 中的摘要/事件仍保留用于封存。
        raw_observations = [item for item in state.get("observations") or [] if isinstance(item, dict)]
        budget = default_context_budget().tool_observation_tokens
        if aggressive:
            budget = max(512, budget // 4)
        per_item_budget = max(160, min(1400, budget // max(len(raw_observations), 1)))
        remaining = budget
        kept_reversed: list[dict[str, Any]] = []
        omitted = 0
        for item in reversed(raw_observations):
            next_item = dict(item)
            observation = next_item.get("observation")
            if isinstance(observation, dict):
                next_observation = dict(observation)
                preview = str(
                    next_observation.get("answer_preview")
                    or next_observation.get("answer")
                    or next_observation.get("content")
                    or ""
                )
                if preview:
                    next_observation["answer_preview"] = clip_to_token_budget(preview, per_item_budget)
                    next_observation.pop("answer", None)
                    next_observation.pop("content", None)
                next_item["observation"] = next_observation
            else:
                next_item["observation"] = clip_to_token_budget(str(observation or ""), per_item_budget)

            serialized = json.dumps(next_item, ensure_ascii=False, default=str)
            cost = estimate_tokens(serialized)
            if cost > remaining and kept_reversed:
                omitted += 1
                continue
            if cost > remaining:
                next_item = {
                    "tool_name": str(item.get("tool_name") or "tool"),
                    "observation": clip_to_token_budget(serialized, max(120, remaining)),
                    "compacted": True,
                }
                cost = estimate_tokens(json.dumps(next_item, ensure_ascii=False))
            kept_reversed.append(next_item)
            remaining = max(0, remaining - cost)
            if remaining <= 0:
                omitted += max(0, len(raw_observations) - len(kept_reversed))
                break

        prompt_observations = list(reversed(kept_reversed))
        if omitted:
            prompt_observations.insert(
                0,
                {
                    "compacted": True,
                    "omitted_old_observations": omitted,
                    "recovery": "完整工具结果保存在本轮 ChatSession transcript，可按 session 读取。",
                },
            )

        prompt_events: list[dict[str, Any]] = []
        for event in self._tool_event_transcript(state):
            summary = {
                key: value
                for key, value in event.items()
                if key
                in {
                    "type",
                    "step",
                    "id",
                    "tool_use_id",
                    "tool_name",
                    "arguments",
                    "status",
                    "route",
                    "error_kind",
                    "retryable",
                    "reason",
                }
            }
            if "arguments" in summary:
                args_text = json.dumps(summary["arguments"], ensure_ascii=False, default=str)
                if estimate_tokens(args_text) > 240:
                    summary["arguments"] = clip_to_token_budget(args_text, 240)
            prompt_events.append(summary)

        original_tokens = estimate_tokens(json.dumps(raw_observations, ensure_ascii=False, default=str))
        prompt_tokens = estimate_tokens(json.dumps(prompt_observations, ensure_ascii=False, default=str))
        return prompt_observations, prompt_events, {
            "mode": "runtime_tool_projection",
            "aggressive": aggressive,
            "observation_budget_tokens": budget,
            "original_observation_tokens": original_tokens,
            "prompt_observation_tokens": prompt_tokens,
            "estimated_tokens_saved": max(0, original_tokens - prompt_tokens),
            "source_observations": len(raw_observations),
            "prompt_observations": len(prompt_observations),
            "omitted_observations": omitted,
            "tool_event_index_entries": len(prompt_events),
        }

    # 优先保留最近消息并裁剪内容，生成上下文溢出重试使用的历史列表。
    def _compress_history_for_retry(
        self,
        history: list[dict[str, str]],
        max_tokens: int,
    ) -> list[dict[str, str]]:
        kept: list[dict[str, str]] = []
        used = 0
        for item in reversed(history):
            content = clip_to_token_budget(item.get("content") or "", max(120, min(700, max_tokens // 3)))
            cost = estimate_tokens(item.get("role")) + estimate_tokens(content)
            if kept and used + cost > max_tokens:
                break
            kept.append({"role": item.get("role") or "user", "content": content})
            used += cost
        return list(reversed(kept))

    # 估算系统提示、用户上下文和工具定义的 token 数，记录压力诊断并返回总量。
    def _record_prompt_diagnostics(
        self,
        state: ConversationState,
        *,
        phase: str,
        system_prompt: str,
        user_prompt: str,
    ) -> int:
        system_tokens = estimate_tokens(system_prompt)
        user_tokens = estimate_tokens(user_prompt)
        tool_schema_tokens = self._tool_schema_estimated_tokens(state)
        total = system_tokens + user_tokens + tool_schema_tokens
        pressure = calculate_context_pressure(total)
        diagnostic = {
            "phase": phase,
            "estimated_prompt_tokens": total,
            "system_tokens": system_tokens,
            "user_context_tokens": user_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "pressure": pressure.as_dict(),
        }
        state["prompt_diagnostics"] = [*(state.get("prompt_diagnostics") or []), diagnostic]
        return total

    # 估算可用工具名称、描述和参数 JSON Schema 占用的 token 总数。
    def _tool_schema_estimated_tokens(self, state: ConversationState) -> int:
        total = 0
        for tool in (state.get("tools") or {}).values():
            total += estimate_tokens(str(getattr(tool, "name", "")))
            total += estimate_tokens(str(getattr(tool, "description", "")))
            args_schema = getattr(tool, "args_schema", None)
            try:
                schema = args_schema.model_json_schema() if args_schema else {}
            except (AttributeError, TypeError, ValueError):
                schema = {}
            total += estimate_tokens(json.dumps(schema, ensure_ascii=False, default=str))
        return total

    # 记录本次模型调用的用量和阶段信息，在缺少实际用量时补充估算值。
    def _record_model_usage(
        self,
        state: ConversationState,
        response: Any,
        *,
        phase: str,
        estimated_prompt_tokens: int,
    ) -> None:
        usage = self._normalize_model_usage(response)
        usage.update(
            {
                "phase": phase,
                "estimated_prompt_tokens": estimated_prompt_tokens,
                "source": "provider" if usage.get("input_tokens") is not None else "estimate",
            }
        )
        if usage.get("input_tokens") is None:
            usage["input_tokens"] = estimated_prompt_tokens
        if usage.get("total_tokens") is None:
            usage["total_tokens"] = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        state["model_usage"] = [*(state.get("model_usage") or []), usage]

    # 从不同格式的模型响应元数据中统一提取 token、缓存用量和模型名称。
    def _normalize_model_usage(self, response: Any) -> dict[str, Any]:
        raw = getattr(response, "usage_metadata", None)
        if raw is not None and not isinstance(raw, dict) and hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        raw = raw if isinstance(raw, dict) else {}
        response_metadata = getattr(response, "response_metadata", None)
        response_metadata = response_metadata if isinstance(response_metadata, dict) else {}
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
        token_usage = token_usage if isinstance(token_usage, dict) else {}
        input_details = raw.get("input_token_details") or {}
        input_details = input_details if isinstance(input_details, dict) else {}
        return {
            "input_tokens": raw.get("input_tokens", token_usage.get("prompt_tokens")),
            "output_tokens": raw.get("output_tokens", token_usage.get("completion_tokens")),
            "total_tokens": raw.get("total_tokens", token_usage.get("total_tokens")),
            "cache_read_input_tokens": input_details.get("cache_read", token_usage.get("cache_read_input_tokens", 0)),
            "cache_creation_input_tokens": input_details.get("cache_creation", token_usage.get("cache_creation_input_tokens", 0)),
            "model": response_metadata.get("model_name") or settings.llm_model,
        }

    # 整理模型用量、提示词诊断及上下文溢出恢复信息，供最终结果返回。
    def _runtime_metadata(self, state: ConversationState) -> dict[str, Any]:
        return {
            "model_usage": state.get("model_usage") or [],
            "context_diagnostics": {
                "prompt_calls": state.get("prompt_diagnostics") or [],
                "runtime_tool_context": state.get("runtime_context_stats") or {},
                "reactive_overflow_retry": bool(state.get("context_overflow_recovery_attempted")),
            },
        }

    # 调用基于现有观察生成回答的流程，作为通用兜底入口。
    async def _fallback_answer(self, state: ConversationState) -> str:
        return await self._final_from_observations(state)

    # 优先读取服务端历史，筛选具有有效文本的用户和助手消息。
    def _history_messages(self, context: dict[str, Any]) -> list[dict[str, str]]:
        raw_messages = context.get("server_messages") or context.get("messages") or []
        if not isinstance(raw_messages, list):
            return []
        messages: list[dict[str, str]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        return messages

    # 读取服务端组装的上下文文本，未提供时返回空字符串。
    def _server_context(self, context: dict[str, Any]) -> str:
        value = context.get("server_context")
        return str(value) if value else ""

    # 读取已注册技能的目录说明，未提供时返回无已注册技能的提示。
    def _registered_skills_context(self, context: dict[str, Any]) -> str:
        value = context.get("registered_skills")
        return str(value) if value else "当前没有注册 DevFlow 技能。"

    # 通过技能注册表按用户消息或显式技能名称激活技能，并返回可序列化的结果。
    def _activate_skills(self, message: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        registry = context.get("_skill_registry")
        if not isinstance(registry, SkillRegistry):
            return []
        requested_name = context.get("requested_skill") or context.get("skill_name")
        activations = registry.activate(
            message,
            requested_name=str(requested_name).strip() if requested_name else None,
        )
        return [activation.model_dump(mode="json") for activation in activations]

    # 从上下文中筛选本轮已激活且格式有效的技能记录。
    def _active_skills(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        skills = context.get("active_skills") or []
        return [item for item in skills if isinstance(item, dict)] if isinstance(skills, list) else []

    # 整合已激活技能的指令、工具约束和资源内容，并按预算裁剪为提示词。
    def _active_skill_prompt(self, context: dict[str, Any]) -> str:
        skills = self._active_skills(context)
        if not skills:
            return "本轮没有匹配到需要加载完整正文的 Skill；仅保留轻量目录摘要。"
        blocks = []
        for skill in skills:
            resources = skill.get("resources") if isinstance(skill.get("resources"), list) else []
            resource_sections = []
            for resource in resources:
                if not isinstance(resource, dict) or not str(resource.get("content") or "").strip():
                    continue
                resource_sections.append(f"### 资源：{resource.get('path')}\n{resource.get('content')}")
            resource_text = "\n\n按需读取的资源：\n" + "\n\n".join(resource_sections) if resource_sections else ""
            blocks.append(
                f"## {skill.get('skill_title')} ({skill.get('skill_name')} v{skill.get('skill_version')})\n"
                f"激活方式：{skill.get('activation_mode')}；原因：{skill.get('activation_reason')}\n"
                f"允许工具：{', '.join(str(item) for item in skill.get('tools') or []) or '无'}\n"
                f"输出契约：{skill.get('output_contract')}；安全级别：{skill.get('safety_level')}\n\n"
                f"{str(skill.get('instructions') or '').strip()}"
                f"{resource_text}"
            )
        return clip_to_token_budget("\n\n".join(blocks), 6000, suffix="\n[Skill 指令因上下文预算被截断]")

    # 检查工具是否在显式激活技能的允许列表中，越权时返回拒绝执行说明。
    def _explicit_skill_tool_violation(self, tool_name: str, state: ConversationState) -> str | None:
        context = state.get("context") or {}
        explicit = [
            skill
            for skill in self._active_skills(context)
            if skill.get("activation_mode") == "explicit"
        ]
        if not explicit:
            return None
        allowed = {
            str(tool)
            for skill in explicit
            for tool in skill.get("tools") or []
        }
        if tool_name in allowed:
            return None
        skill_names = "、".join(str(skill.get("skill_name")) for skill in explicit)
        return f"工具 {tool_name} 不在显式 Skill（{skill_names}）声明的允许工具列表中，已拒绝执行。"

    # 结合实际工具和委派任务事件，记录技能执行状态、入口结果及工具契约违规情况。
    def _record_skill_validation(self, state: ConversationState) -> None:
        context = state.get("context") or {}
        active = self._active_skills(context)
        if not active:
            return
        observed_tools = [
            str(event.get("tool_name") or "")
            for event in state.get("agent_events") or []
            if isinstance(event, dict) and event.get("type") == "tool_use" and event.get("tool_name")
        ]
        successful_tools = [
            str(event.get("tool_name") or "")
            for event in state.get("agent_events") or []
            if isinstance(event, dict)
            and event.get("type") == "tool_result"
            and event.get("status") == "success"
            and event.get("tool_name")
        ]
        explicit_allowed = {
            str(tool)
            for skill in active
            if skill.get("activation_mode") == "explicit"
            for tool in skill.get("tools") or []
        }
        explicit_violations = [tool for tool in observed_tools if explicit_allowed and tool not in explicit_allowed]
        for skill in active:
            allowed = {str(tool) for tool in skill.get("tools") or []}
            used = [tool for tool in observed_tools if tool in allowed]
            entrypoint_called = str(skill.get("entrypoint") or "") in used
            successful_used = [tool for tool in successful_tools if tool in allowed]
            entrypoint_succeeded = str(skill.get("entrypoint") or "") in successful_used
            delegated_tasks = [
                event
                for event in state.get("agent_events") or []
                if isinstance(event, dict)
                and event.get("type") == "workflow_task_result"
                and event.get("skill_name") == skill.get("skill_name")
                and event.get("status") == "success"
            ]
            if explicit_violations and skill.get("activation_mode") == "explicit":
                status = "violated"
            elif entrypoint_succeeded or successful_used or delegated_tasks:
                status = "completed"
            elif used:
                status = "failed"
            else:
                status = "not_executed"
            self._append_agent_event(
                state,
                "skill_validation",
                {
                    "skill_name": skill.get("skill_name"),
                    "skill_title": skill.get("skill_title"),
                    "skill_version": skill.get("skill_version"),
                    "status": status,
                    "instructions_loaded": bool(str(skill.get("instructions") or "").strip()),
                    "instruction_digest": skill.get("instruction_digest"),
                    "declared_steps": skill.get("workflow_steps") or [],
                    "observed_tools": used,
                    "entrypoint_called": entrypoint_called,
                    "entrypoint_succeeded": entrypoint_succeeded,
                    "delegated_task_ids": [str(event.get("task_id") or "") for event in delegated_tasks],
                    "tool_contract_violations": explicit_violations if skill.get("activation_mode") == "explicit" else [],
                    "step_verification": (
                        "entrypoint_completed"
                        if entrypoint_succeeded
                        else "delegated_entrypoint_completed"
                        if delegated_tasks
                        else "supporting_tools_observed"
                        if successful_used
                        else "skill_tool_failed"
                        if used
                        else "no_skill_tool_observed"
                    ),
                },
            )

    # 为工具事件匹配所属技能，提取技能版本、步骤和激活方式等追踪信息。
    def _skill_event_metadata(self, tool_name: str, state: ConversationState) -> dict[str, Any]:
        context = state.get("context") or {}
        active = self._active_skills(context)
        selected = next((skill for skill in active if skill.get("entrypoint") == tool_name), None)
        if selected is None:
            selected = next((skill for skill in active if tool_name in (skill.get("tools") or [])), None)
        if selected is None:
            selected = next((skill for skill in active if skill.get("activation_mode") == "explicit"), None)
        if selected is not None:
            return {
                "skill_name": selected.get("skill_name"),
                "skill_title": selected.get("skill_title"),
                "skill_version": selected.get("skill_version"),
                "skill_steps": selected.get("workflow_steps") or [],
                "skill_activation_mode": selected.get("activation_mode"),
                "skill_activation_reason": selected.get("activation_reason"),
                "skill_instruction_digest": selected.get("instruction_digest"),
            }
        mapping = context.get("skill_trace_by_tool") or {}
        if not isinstance(mapping, dict):
            return {}
        metadata = mapping.get(tool_name)
        if not isinstance(metadata, dict):
            return {}
        return {
            key: value
            for key, value in metadata.items()
            if key in {"skill_name", "skill_title", "skill_version", "skill_steps"}
        }

    # 将有效团队成员信息序列化为分工提示上下文，缺少成员时给出说明。
    def _team_assignment_context(self, context: dict[str, Any]) -> str:
        members = self._team_members(context)
        if not members:
            return "team_members: []（当前仓库还没有配置 DevFlow 团队成员。）"
        return "team_members: " + json.dumps(members, ensure_ascii=False)

    # 筛选和规范化团队成员的姓名、角色、专长及技术栈，最多保留二十名成员。
    def _team_members(self, context: dict[str, Any]) -> list[dict[str, str]]:
        raw_members = context.get("team_members") or []
        if not isinstance(raw_members, list):
            return []
        members: list[dict[str, str]] = []
        for member in raw_members:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            if not name:
                continue
            members.append(
                {
                    "name": name,
                    "role": str(member.get("role") or "").strip(),
                    "strengths": str(member.get("strengths") or "").strip(),
                    "techStack": str(member.get("techStack") or member.get("tech_stack") or "").strip(),
                }
            )
        return members[:20]

    # 按工具类型裁剪观察正文，返回预览、路由、压缩统计和引用数量。
    def _summarize_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        # 第一次限长：工作区结果用整份工具预算，其他结果用约 1/3（至少 300 token）。
        # 只保存 answer_preview、路由、引用数量与压缩统计，不保存完整 observation 字典。
        answer = str(observation.get("answer") or "")
        budget = default_context_budget()
        limit = budget.tool_observation_tokens if str(observation.get("route") or "").startswith("workspace_") else max(300, budget.tool_observation_tokens // 3)
        compressed = clip_to_token_budget(answer, limit)
        return {
            "route": observation.get("route"),
            "answer_preview": compressed,
            "compression": {
                "mode": "tool_observation_budget",
                "original_estimated_tokens": estimate_tokens(answer),
                "compressed_estimated_tokens": estimate_tokens(compressed),
                "budget_tokens": limit,
            },
            "citations_count": len(observation.get("citations", [])),
        }

    # 根据预取证据初始化评估轨迹，记录检索文本、来源和预取查询。
    def _initial_evaluation_trace(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        evidence = context.get("evidence") if isinstance(context.get("evidence"), list) else []
        contexts: list[str] = []
        sources: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            rendered_context = self._render_evaluation_context(item)
            if rendered_context:
                contexts.append(rendered_context)
            sources.append(
                {
                    "id": str(item.get("id") or ""),
                    "source_id": str(item.get("source_id") or ""),
                    "source_type": str(item.get("source_type") or ""),
                    "title": str(item.get("title") or ""),
                    "score": item.get("score"),
                }
            )
        retrievals = []
        if contexts or sources:
            retrievals.append(
                {
                    "stage": "prefetch",
                    "query": message,
                    "contexts": contexts,
                    "sources": sources,
                }
            )
        return {
            "target": "ChatAgent",
            "retrievals": retrievals,
            "tool_observations": [],
        }

    # 在评估模式下记录工具调用结果，并补充检索查询、文本及来源信息。
    def _capture_evaluation_observation(
        self,
        state: ConversationState,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        if not (state.get("context") or {}).get("evaluation_mode"):
            return
        trace = dict(state.get("evaluation_trace") or {})
        tool_observations = list(trace.get("tool_observations") or [])
        tool_observations.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "status": "error"
                if any(
                    call.get("status") == "error"
                    for call in observation.get("tool_calls", [])
                    if isinstance(call, dict)
                )
                else "success",
                "content": str(observation.get("answer") or ""),
            }
        )
        trace["tool_observations"] = tool_observations

        raw_sources = observation.get("retrieval_results")
        sources = [dict(item) for item in raw_sources if isinstance(item, dict)] if isinstance(raw_sources, list) else []
        contexts = [self._render_evaluation_context(item) for item in sources]
        contexts = [item for item in contexts if item]
        if not contexts:
            contexts = [
                str(item).strip()
                for item in observation.get("retrieved_contexts", [])
                if str(item).strip()
            ]
        if tool_name == "rag.search_similar_documents" or contexts or sources:
            retrievals = list(trace.get("retrievals") or [])
            retrievals.append(
                {
                    "stage": "agent_tool",
                    "query": str(arguments.get("query") or arguments.get("message") or ""),
                    "contexts": contexts,
                    "sources": sources,
                }
            )
            trace["retrievals"] = retrievals
        state["evaluation_trace"] = trace

    # 将证据来源类型、标题和正文片段拼接为统一的评估上下文文本。
    @staticmethod
    def _render_evaluation_context(item: dict[str, Any]) -> str:
        """Render the same title/source/snippet evidence shape shown to ChatAgent."""

        source_type = str(item.get("source_type") or "").strip()
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        heading = " ".join(part for part in ([f"[{source_type}]" if source_type else "", title]) if part)
        if heading and snippet:
            return f"{heading}: {snippet}"
        return heading or snippet

    # 在评估模式下合并去重检索文本，并附加工具轨迹生成评估结果。
    def _evaluation_result(self, state: ConversationState) -> dict[str, Any]:
        if not (state.get("context") or {}).get("evaluation_mode"):
            return {}
        trace = dict(state.get("evaluation_trace") or {})
        contexts: list[str] = []
        seen: set[str] = set()
        for retrieval in trace.get("retrievals") or []:
            if not isinstance(retrieval, dict):
                continue
            for item in retrieval.get("contexts") or []:
                text = str(item).strip()
                if text and text not in seen:
                    seen.add(text)
                    contexts.append(text)
        trace["retrieved_contexts"] = contexts
        trace["agent_events"] = self._tool_event_transcript(state)
        return {"evaluation_trace": trace}

    # 从模型消息的字符串或分块内容中提取并拼接纯文本。
    def _message_content(self, message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "".join(parts)
        return str(content or "")

    # 将完整回答切分为小段，逐段输出 delta 流式事件。
    async def _stream_answer(self, answer: str) -> AsyncIterator[dict[str, Any]]:
        for part in self._chunk_text(answer):
            yield self._event("delta", {"content": part})

    # 按指定字符数切分文本，默认每段三十六个字符。
    def _chunk_text(self, text: str, size: int = 36) -> list[str]:
        if not text:
            return []
        return [text[index : index + size] for index in range(0, len(text), size)]

    # 将事件名称和数据封装为统一的流式事件字典。
    def _event(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"event": event, "data": data}


def encode_sse(event: dict[str, Any]) -> str:
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
