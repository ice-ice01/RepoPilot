# 上下文与记忆：完整调用链与 Python 阅读指南

这份说明依据当前代码实现，对照六层设计理解。截图是设计参考，代码才是实际行为的依据。

## 1. 先区分四种“记忆”

模型本身不会替项目永久保存聊天。每次调用时，后端重新读取数据库、挑选相关信息，再放入提示词。

| 概念 | 对应对象 | 用途 | 是否等于完整原文 |
| --- | --- | --- | --- |
| 原始消息 | `ChatMessage` | 保存用户/助手消息；支持历史查询和精确回忆 | 用户/助手正文保留原文 |
| 本次上下文 | `AssembledContext`、Agent 提示词 | 给当前模型请求看的工作窗口 | 否，会筛选和裁剪 |
| 自动会话记忆 | `ConversationMemory` | 保存当前聊天的摘要、事实、任务、决策、偏好 | 否，是结构化提炼结果 |
| 项目长期知识 | 批准后的 `Document(source_type="memory_note")` | 可通过仓库 RAG 检索的长期信息 | 保存批准的候选内容，不是全部聊天 |

还有两个容易混淆的名字：

- `Conversation` 是侧边栏里的一整个聊天；正常记忆按 `conversation_id` 隔离。
- `ChatSession` 是一次用户提问加一次助手回答的封存轮次，包含问答、工具事件、用量、摘要。
- `db: Session` 是 SQLAlchemy 数据库连接/事务对象，与上述聊天概念无关。
- `ThreadMemory` 是按仓库存储的兼容旧路径；正常带仓库和聊天 ID 的流程使用 `ConversationMemory`。

## 2. 从 HTTP 请求串起整条链路

入口在 `backend/app/api/routes/chat.py`：`chat_stream()` 调用 `_conversation_sse()`；非流式 `chat()` 调用 `_conversation_response()`。计划执行流还有 `_conversation_plan_execute_sse()`，也使用相同的组装、封存组件。

```text
前端 POST /api/chat/stream
  -> ensure_conversation：确定仓库内的聊天
  -> ProgressiveContextManager.ensure_headroom：请求前检查压力，必要时写摘要边界
  -> MessageStore.append：保存本轮用户原文
  -> EvidenceStore.rebuild_repo：重建仓库/当前聊天的证据集合
  -> commit：让后续 MCP 子进程能读到已提交的本轮问题
  -> ContextAssembler.assemble：读取摘要、记忆、历史、检索证据并控制预算
  -> _build_conversation_tools：绑定工作区、RAG、MCP 等工具
  -> ChatAgent.run_stream
       -> 近期原文足够：直接回答回忆问题
       -> 需要指定记忆工具：先取原文/封存轮次/证据，再生成回答
       -> 一般工具循环：模型判断 -> 工具执行 -> 结果投影 -> 再判断
       -> 模型判断超长：缩小提示词，仅重试这次调用一次
  -> 收到 Agent final 事件
  -> MessageStore.append：保存助手回答、工具事件与诊断
  -> 保存 AgentRun，更新 Conversation 消息计数和标题
  -> SessionSealer.seal：封存这一轮并提炼、更新会话记忆，生成待批准候选
  -> commit
  -> final 事件发给前端
```

因此记忆更新发生在 Agent 已产出最终回答之后，但在路由提交并把最终事件发给前端之前；不是一个独立后台记忆任务。之前的中间事件可以先发送。

客户端提交的 `server_context`、`server_messages`、`evidence`、`tools`、仓库/聊天 ID 等字段会被 `_client_context()` 排除，最终由后端提供，避免客户端覆盖服务端的记忆范围。

## 3. 第 1 层：数据库完整时间线

代码：`backend/app/db/models.py` 的 `ChatMessage`；`backend/app/services/chat_memory.py` 的 `MessageStore`。

`append()` 新建消息并 `flush()`。`flush()` 取得主键并把变更写入当前事务，不等于已经永久提交；外层路由 `commit()` 后才持久生效。

`timeline()` 是完整原始时间线的读取入口：

- 按仓库和聊天过滤。
- 默认隐藏 `system` 摘要边界，以及旧实现产生的 `preserved_after_compact` 兼容副本。
- 按 `created_at`、`id` 排序；时间相同时用 ID 保持游标顺序稳定。
- 支持 `before_message_id`、`after_message_id` 和数量限制。
- 当前实现先查出匹配记录，再在 Python 列表中取一页，不是数据库级 `LIMIT` 分页。

`GET /api/chat/history` 使用 `timeline()`，所以压缩后前端仍可以查看旧消息。“保留原文”指压缩不会删除它；用户显式删除 Conversation 是另一条会删除关联记录的流程。

## 4. 第 2 层：活动上下文投影

代码：`MessageStore.recent()`、`last_compact_boundary()`、`ContextAssembler.assemble()`。

没有摘要边界时，`recent()` 返回最近的时间线记录；有边界时构造：

```text
最新 compact_boundary
  + preserved_message_ids 所引用的旧原消息
  + 边界之后新增的消息
```

最后再取最近 `limit` 条。不会复制保留消息的正文，也不会删除边界之前的原文。组装器另外单独查询最新 boundary，所以边界即使不在最近列表的尾部窗口中，也可以注入摘要。

`ContextAssembler.assemble()` 读取：

1. 仓库和聊天名称。
2. 最新压缩边界中的摘要、`rehydration`、`transcript_ref`。
3. 当前 `ConversationMemory`，无聊天范围时兼容 `ThreadMemory`。
4. 最近 `sealed` 或 `sealed_with_warnings` 轮次的 digest。
5. 针对当前问题检索的最多 8 条证据。
6. 活动窗口近期用户/助手消息，再按历史预算裁剪。

返回 `system_context`、`history_messages`、`evidence` 和 `compression_stats`。名称虽叫 `system_context`，普通工具循环实际把它嵌入一个 `HumanMessage` 的文本里；稳定控制规则才是 `SystemMessage`。

默认 `assemble(limit=16)` 并非严格只查 16 条；代码用 `max(limit, 30)`，至少读取 30 条活动记录。每条历史通常最多约 900 个估算 token。

`history_coverage` 记录遗漏、截断、精确尾部长度、有无边界等。Agent 用它判断“最近消息是否足以可靠回答”，不能把摘要当逐字原文。

## 5. 第 3 层：请求前预算和压力

代码：`backend/app/services/context_compression.py` 的 `estimate_tokens()`、`ContextBudget`、`default_context_budget()`、`calculate_context_pressure()`；默认配置在 `backend/app/core/config.py`，示例覆盖值在 `.env.example`。

token 是模型处理文本的长度单位，不等于字符数。本项目采用近似计数：中文单字约 1，英文/标识符约 4 字符为 1，其他符号约 1；没有调用精确模型 tokenizer。

先预留响应空间，再把输入分为：

| 分类 | 默认比例 | 内容 |
| --- | --- | --- |
| system | 12% | 组装器里的仓库/聊天标识 |
| memory | 22% | 边界摘要、复水状态、会话记忆、上一轮 digest |
| evidence | 34% | 当前问题相关证据 |
| recent | 22% | 近期用户/助手消息 |
| tool | 10% | 本轮工具观察摘要 |

`context_reserved_response_tokens` 默认 3000，`context_max_input_tokens` 默认 24000。但默认启用动态窗口：使用 `max(24000, 模型表中的窗口)`，所以 24000 不是固定硬上限。模型表是项目本地估计，不代表提供方真实能力，且不会自动查询。

纯粹按代码默认值举例：`deepseek-v4-pro` 在表里是 1,000,000；普通预算先扣 3000，剩 997,000 再分块。压缩压力另预留 12000，得到有效窗口 988,000，默认阈值为 warning 955,000、auto 975,000、aggressive 980,000、manual 985,000。实际环境覆盖和模型名称会改变结果。

`ensure_headroom()` 同时计算活动原记录规模和组装后规模，取较大的压力。进入 `normal`/`warning` 一般不正式压缩；进入后三个阶段会尝试正式压缩。连续压缩失败默认达到 3 次后启用熔断。

这些是分块预算和近似诊断，不是最终请求绝不超限的保证：实际提示词还包含固定规则、技能指令、团队信息、工具 schema、JSON 包装等。本轮问题通常已经在历史中，最终提示词还会单列“用户消息”。

## 6. 第 4 层：轻量治理

这里有三个不同动作，不要都叫“生成摘要”。

### 历史窗口裁剪

`compress_messages()` 从新到旧保留用户/助手消息，单条限长、总量限长，然后恢复时间顺序。纯本地字符串处理，不调用 LLM，不写数据库。

### Microcompact

`microcompact_messages()` 保护最近消息，只把较早、足够长、可再次取得的工具结果改成来源、恢复提示和短摘录。只处理 `tool`/`tool_result` 或明确标记为工具结果的记录，不改写助手最终答案，也不覆盖输入原记录。

重要实现差异：`ContextAssembler` 目前只把 user/assistant 放入 `raw_history`，因此普通聊天在这个位置通常没有符合条件的工具结果，Microcompact 会返回 0 条清理。它是已有算法，但不能据此认为常规历史会实际通过它节省空间。

### 工具结果两次投影

代码在 `backend/app/services/agents/chat_agent.py`：

1. `_summarize_observation()` 对单个工具返回的 answer 先裁剪，保存 `answer_preview`、路由、引用数量、压缩统计。
2. `_runtime_tool_context()` 每次模型判断前，再对已记录的工具摘要按总预算投影，新结果优先；事件索引保留工具名、参数和状态，参数过长也裁剪。

第二次投影不会改变 `state` 中已记录摘要，但第一次裁剪可能已经舍弃工具原始大正文。`ChatSession.transcript_json` 保存的是已记录的工具事件，不能保证能从封存记录找回工具返回的全部正文；需要时应重读工作区、重新检索或查原数据。

## 7. 第 5 层：正式压缩与复水

代码：`ProgressiveContextManager._compact()`、`_messages_to_keep()`、`_summary_for()`、`_insert_boundary()`、`_rehydration_state()`。

流程如下：

1. 读取最新边界对应的活动片段，筛选用户/助手/工具等候选记录。
2. 普通方案保留最近 8 条、摘要约 1800 token；压力仍高时升级 aggressive，再升级 manual_path。默认约为保留 4 条/摘要 1206，保留 2 条/摘要 900。
3. 调整切点，保护工具调用与结果配对、同一提供方消息的分块、必要的相邻用户问答。实际保留条数可能多于配置。
4. 有现成结构化会话记忆时优先复用，否则请求 LLM 生成旧片段摘要；LLM 返回空内容则规则抽取兜底。抛出未处理异常时走失败记录/熔断路径。
5. 检查摘要是否漏了路径、命令、错误、偏好、待办等关键原文，尽力补充，记录保真报告。预算有限，不保证所有信息都不丢。
6. 计算摘要、保留原消息、当前问题、复水状态的新规模；若还太大，换更紧的方案重算。
7. 只写一条 `system` 消息，`meta.subtype="compact_boundary"`。保存摘要范围、保留消息 ID、原文恢复入口、保真报告和复水信息。
8. 下一次 `recent()` 使用新边界构造活动窗口；`timeline()` 仍能读取旧原文。

`manual_path` 是代码中的最紧自动方案名称，不会在这里暂停等待人工操作。最后方案仍过大时，代码也可能保存边界，并不是保证已经低于阈值才保存。

复水状态只保留最近约 5 个文件路径、6 个技能名称、8 个工具名称，以及回忆工具入口。它是继续工作的线索，不是把文件内容、技能完整正文或 Agent 执行栈原样恢复。

`transcript_ref` 指向仓库、聊天和 `devflow_get_thread_context`，不是另存了一份文件转录。

## 8. 第 6 层：超长恢复与回答后记忆

### 被动超长恢复

`ChatAgent._graph_model_decision()` 识别 `context_length_exceeded`、`prompt too long` 等报错：

- 标记 `context_overflow_recovery_attempted=True`。
- 工具观察预算缩到四分之一，近期历史预算减半，并缩短 server_context。
- 仅重试当前失败的模型调用，不重跑之前的工具。
- 这轮再次超长则抛出，不无限重试；也不会因此写入正式压缩边界。

这个机制位于常规模型判断路径，不能理解为所有 LLM 调用都有相同恢复：近期直接回忆、依据工具结果生成结论、记忆提炼等路径有各自处理方式。

### SessionSealer：一轮结束后更新会话记忆

1. 先用规则构造兜底摘要和字段。
2. 新建 `ChatSession`，写问答、工具调用/事件、模型用量、上下文诊断；状态为 sealing，然后 flush。
3. 向模型输入旧记忆和本轮记录，要求返回完整 JSON 快照：digest、summary、facts、decisions、open_questions、tasks、user_preferences、repo_context、citations。
4. 模型成功：标记 snapshot，替换会话记忆字段；模型不可用或提炼失败：规则更新并合并旧记忆。摘要约限 1100 token，数组按类别限 12/16 条。
5. 在嵌套事务中更新 `ConversationMemory`，并生成 `MemoryCandidate(pending)`。失败时回滚这部分，尽量保留已创建的 ChatSession。
6. 完成后状态为 sealed 或 sealed_with_warnings，外层路由最终 commit。

“先存原轮次”指先进入当前事务，并非在模型提炼之前已经独立 commit。JSON 字段变更通过复制字典并重新赋值，让 SQLAlchemy 检测到修改。

规则兜底合并按旧内容在前、新内容在后去重并限长；满额时可能留旧丢新，不能依赖结构化摘要实现无限期、无损回忆。

### 长期记忆是另一路审批

```text
本轮 memory_update
  -> capture_memory_candidates：决策/事实/任务/偏好/项目上下文，去重，pending
  -> 用户批准：POST /api/repos/{repo_id}/memory/candidates/{candidate_id}/approve
  -> approve_memory_candidate
  -> add_document(source_type="memory_note")
  -> embedding + Document + 启用时写 Milvus
  -> 重建证据、提交
  -> 后续该仓库的 RAG 可以检索此条知识
```

普通 `ConversationMemory` 的更新不需要人工批准，但也不会自动变成向量库知识。拒绝候选只改变候选状态，不等于撤销已经存在的自动会话记忆。

批准流程代码在 `backend/app/services/memory_hub.py`、`backend/app/api/routes/knowledge.py`；向量入库在 `backend/app/services/rag/vector_store.py`。SQL 与 Milvus 不是同一个事务，不能认为一次 commit 能同时保证两个存储原子成功。

## 9. 需要原文时，MCP 怎么读取？

```text
ChatAgent 选择记忆工具
  -> chat.py / _build_conversation_tools / call_memory_mcp
  -> StdioMcpClient.call_tool
  -> 启动 app/mcp/memory_server.py 子进程
  -> initialize + tools/call（stdin/stdout 按行 JSON-RPC）
  -> _call_memory_tool：独立数据库连接
  -> MemoryTools 对应方法
  -> 查数据库 + 写 RecallEvent + commit
  -> MCP 返回文本 -> Agent 工具结果投影 -> 回答
```

每次 MCP 调用都会创建并关闭子进程，当前不是常驻连接池。

| 工具 | 底层方法 | 合适问题 |
| --- | --- | --- |
| `devflow_get_thread_context` | `MemoryTools.get_thread_context()` | 聊天原话、压缩前记录、精确消息 ID；支持字符分段 |
| `devflow_read_session_events` | `MemoryTools.read_session_events()` | 某一封存轮次的问答、摘要、已记录工具轨迹 |
| `devflow_search_evidence` | `MemoryTools.search_evidence()` -> `EvidenceStore.search()` | 仓库知识、历史决策、相关证据 |

原文工具通过完整 `timeline()` 或消息 ID 查询，可跨摘要边界；长消息返回 next_offset 提示，需继续读取，而非一次返回无限正文。

两个当前限制：

- session 工具仍按当前 Conversation 过滤；提示词提到“跨会话”，不代表当前实现能自动访问其他聊天。
- `MemoryTools.read_session_events()` 支持 offset，但 MCP schema 和转发入口没有暴露/传递它，当前 MCP 无法按此参数续读长 session。thread_context 的 offset 则已完整透传。

证据检索先取仓库 RAG，数量不足时再用 EvidenceItem 的关键词匹配补足；它不是把全部结果再做统一全局排序，也不保证所有基础设施故障都能自动降级。

## 10. 不懂 Python 时按这个顺序读

1. `backend/app/api/routes/chat.py`：先看 `_conversation_sse()`，理解调用顺序。
2. `backend/app/db/models.py`：看 ChatMessage、ChatSession、ConversationMemory 的数据区别。
3. `backend/app/services/chat_memory.py`：看 MessageStore、ContextAssembler、ProgressiveContextManager、SessionSealer。
4. `backend/app/services/context_compression.py`：看预算、裁剪、保真、记忆合并的纯算法。
5. `backend/app/services/agents/chat_agent.py`：看 `_graph_model_decision()`、`_summarize_observation()`、`_runtime_tool_context()`、回忆路由策略。
6. `backend/app/services/mcp_client.py` 和 `backend/app/mcp/memory_server.py`：看原文恢复如何穿过子进程。
7. `backend/app/services/memory_hub.py`：看长期候选与审批，不要和自动会话记忆混在一起。

Python 语法速查：

```python
def recent(self, limit: int = 20) -> list[ChatMessage]:
    # def = 定义方法；self 类似 Java 的 this；limit 默认 20；-> 是返回类型说明。
    # 缩进决定代码块，没有 Java 的花括号。类型说明本身不是运行时校验。
    return rows[-limit:]  # 列表切片：取最后 limit 个元素，不删除原列表。

async def run(...):
    result = await llm.chat_json(...)  # 等待网络操作；不是自动新开一个后台线程。
    yield event  # 逐次产出事件，调用方可用 async for 消费。

value = data.get("facts") or []  # 键不存在/值为空时，用空列表。
copy = {**old, "summary": text}  # 复制旧字典，再覆盖 summary，类似 Map 合并。
rows = [item for item in source if item.role == "user"]  # 过滤 + 映射，类似 stream。

with db.begin_nested():
    update_memory()  # 进入/退出事务作用域；这里是数据库 SAVEPOINT。
```

`db.query(Model).filter(...).order_by(...).all()` 可以按“查询实体 -> WHERE -> ORDER BY -> 返回列表”理解；`first()` 返回第一条或 None，`one_or_none()` 要求最多一条。

已有验证用例位于 `backend/app/tests/test_context_compression.py`、`test_context_memory_projection.py`、`test_chat_agent.py`，覆盖边界前原文读取、保留原消息引用、工具配对、复水、提炼失败保留轮次、超长仅重试一次等行为。
