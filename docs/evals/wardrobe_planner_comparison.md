# ice-ice01/wardrobeAgent：真实项目 Planner 对照实验

模型：gpt-6.1-sol；用例：20；每例重复：2。

只测首次规划中的 Agent 选择和任务图，不执行专用 Agent，不代表最终回答质量或重规划效果。

## 结论

这组真实项目诊断题中，模型规划＋程序校验＋规则兜底完全选对 36/40 次（90%），现有规则为 14/40 次（35%）。20 个问题各重复两轮，不是 40 个独立问题。问题和预期标签由本次实验预先编写，不能视为真实用户流量准确率。

成功返回并通过校验的 32 次模型规划中，31 次完全正确（96.9%）。另外 8 次为 ReadTimeout，全部回退到现有规则，回退中有 3 次选择错误。因此主要结果必须使用含回退的 90%，不能用 96.9% 代替整体效果。

混合方案中位规划耗时 30.61 秒，P95 为 61.83 秒；规则中位耗时约 0.05 毫秒。超时率 20%，当前不适合在没有进一步延迟和可用性优化的情况下直接切换为默认路径。真实摘要增加了输入规模，但接口负载和请求条件也会影响耗时，不能将本轮延迟全部归因于输入变长。

模型唯一一次语义多选是 P4 第二轮：要求对照文档审查 PR 时，它额外加入 CI 检查。按冻结标签计为错误，但产品上也可能将这视为有用的补充验证；正式应用需要先明确是否允许这类扩展。没有因为看到结果而修改标签或重跑筛选成绩。

80 份计划（两策略各 40 份）均通过真实仓库目标绑定检查，未错绑到其他仓库、虚构编号或较新的不相关对象；3 个不存在目标也均在真实入口被拒绝。没有发生程序补入强制任务的修复。本组未包含写入用例，不能据此评价写入安全识别。

建议继续采用“模型规划＋程序硬约束＋规则故障兜底”的方向，但先改进输入摘要、可配置超时、等待中的界面反馈和故障回退体验；这些优化尚未实施或验证。还需要专用 Agent 执行与最终回答的端到端测试。本次仅新增隔离实验、测试和报告，正式 Planner 和业务数据未修改。

## 本次使用的真实数据

最近同步时间：2026-10-06 19:33:07 北京时间（未重新查询 GitHub）。

快照：{'issues': 1, 'pull_requests': 1, 'workflow_runs': 5, 'documents': 49, 'knowledge_source_documents': 2}。

- Issue #1：默认形象支持删除，状态 closed。
- PR #2：feat: allow deleting default user models，合入时间 2026-10-02 19:47:59 北京时间。
- CI #37002998264：main / success / successful_rerun。
- CI #37000377466：feature/default-model-delete / success / successful_rerun。
- CI #36998838302：feature/default-model-delete / failure / superseded_by_success。
- CI #36995268346：main / failure / superseded_by_success。
- CI #35232160204：main / failure / superseded_by_success。
- 文档 AI-Agent-工具调用与穿搭生成流程.md：completed，23 段。
- 文档 AI智能衣橱穿搭Agent-PRD-v1.8.md：embedding，138 段。

旧失败运行均应与后续成功运行区分；向量化中的 PRD 不被当成已可用的检索证据。

问题和预期答案由本次实验编写，但编号、标题、对象状态、修改文件、CI 结果和引用文档来自该真实项目，不是替换虚构项目名称。

## 模型偏差

- G2 第 1 轮：多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search']，漏选 []；rule_fallback。
- G4 第 2 轮：多选 ['ci_debug', 'rag_search']，漏选 []；rule_fallback。
- P4 第 2 轮：多选 ['ci_debug']，漏选 []；llm。
- I2 第 2 轮：多选 ['ci_debug', 'pr_review', 'rag_search']，漏选 ['issue_analysis']；rule_fallback。

模型成功返回且通过校验的请求：32；其中 Agent 集合完全正确：31（96.9%）。
规则回退请求：8。下面的主表统计混合方案整体，不把回退结果冒充模型原生判断。

真实入口的不存在目标保护：3/3 正确拒绝，不进入模型。

不能将此结果解释为模型已经正确回答了真实 Issue 的优先级、代码风险或 CI 根因；这些是专用 Agent 的后续任务。

| 指标 | 现有规则 | 模型＋校验＋规则兜底 |
|---|---:|---:|
| Agent 集合完全正确 | 35.0% | 90.0% |
| 专用任务精确率 | 46.2% | 83.6% |
| 专用任务召回率 | 69.2% | 98.1% |
| F1 | 55.4% | 90.3% |
| 多选任务总数 | 42 | 10 |
| 漏选任务总数 | 16 | 1 |
| 计划结构/对象合法率 | 100.0% | 100.0% |
| 强制安全门（本组无写入用例） | 未测 | 未测 |
| 耗时中位数 ms | 0.05 | 30611.36 |
| 耗时 P95 ms | 0.09 | 61825.39 |
| 规则兜底次数 | 0 | 8 |

## 分类正确率

| 类别 | 规则 | 混合方案 |
|---|---:|---:|
| 真实 Issue | 2/8 | 7/8 |
| 真实 PR | 4/8 | 7/8 |
| 真实 CI | 2/8 | 8/8 |
| 真实文档 | 4/8 | 8/8 |
| 真实跨域与边界 | 2/8 | 6/8 |

混合方案输出的 Agent 集合重复一致：16/20（至少两轮才有意义）。
接口报告总 token：273468；不估算未核实单价的费用。
程序补入强制任务次数：0。

## 每例结果（不同轮次保留全部记录）

| 用例 | 用户问题 | 预期专用任务 | 规则 | 混合方案 |
|---|---|---|---|---|
| I1 | 回顾 Issue #1“默认形象支持删除”的优先级和负责人建议。 | issue_analysis | 正确；正确 | 正确（兜底）；正确 |
| I2 | 只回顾工单 #1 的分诊，不 review PR，也不看 CI 或历史文档。 | issue_analysis | 多选 ['ci_debug', 'pr_review', 'rag_search'] 漏选 ['issue_analysis']；多选 ['ci_debug', 'pr_review', 'rag_search'] 漏选 ['issue_analysis'] | 正确；多选 ['ci_debug', 'pr_review', 'rag_search'] 漏选 ['issue_analysis']（兜底） |
| I3 | 用户提出“默认形象也希望支持删除”，请回顾这条反馈应该如何分诊。 | issue_analysis | 多选 ['pr_review'] 漏选 ['issue_analysis']；多选 ['pr_review'] 漏选 ['issue_analysis'] | 正确；正确 |
| I4 | 结合已上传的项目文档，回顾 Issue #1 默认形象删除诉求及验收标准。 | issue_analysis, rag_search | 多选 [] 漏选 ['rag_search']；多选 [] 漏选 ['rag_search'] | 正确；正确 |
| P1 | 回顾审查 PR #2 的 diff。 | pr_review | 正确；正确 | 正确（兜底）；正确 |
| P2 | 只回顾审查 PR #2，不要分析 Issue、CI，也不要查历史。 | pr_review | 多选 ['ci_debug', 'issue_analysis', 'rag_search'] 漏选 []；多选 ['ci_debug', 'issue_analysis', 'rag_search'] 漏选 [] | 正确；正确 |
| P3 | 检查 UserModelService.java 中默认形象删除改动有没有隐患，只做代码审查。 | pr_review | 正确；正确 | 正确；正确 |
| P4 | 对照已上传的 Tool Calling 与穿搭生成流程文档，回顾 PR #2 的默认形象删除改动是否影响该流程。 | pr_review, rag_search | 多选 [] 漏选 ['rag_search']；多选 [] 漏选 ['rag_search'] | 正确；多选 ['ci_debug'] 漏选 [] |
| C1 | 回顾 CI #36998838302 当次失败的原因，仅分析该次运行，不判断现在仍失败。 | ci_debug | 正确；正确 | 正确（兜底）；正确 |
| C2 | 只检查 CI #37002998264 的结果，不分析 Issue 或 PR。 | ci_debug | 多选 ['issue_analysis', 'pr_review'] 漏选 []；多选 ['issue_analysis', 'pr_review'] 漏选 [] | 正确；正确 |
| C3 | main 分支最近一次自动检查是否通过？请分析现有运行记录，不审查代码改动。 | ci_debug | 多选 ['pr_review'] 漏选 ['ci_debug']；多选 ['pr_review'] 漏选 ['ci_debug'] | 正确；正确 |
| C4 | 检索既往故障经验，并回顾 CI #36998838302 的失败经过，区分旧故障与后续通过状态。 | ci_debug, rag_search | 多选 [] 漏选 ['rag_search']；多选 [] 漏选 ['rag_search'] | 正确；正确 |
| D1 | 在知识库的《AI-Agent-工具调用与穿搭生成流程.md》中查 generateOutfit 工具怎样取得衣服数据。 | rag_search | 正确；正确 | 正确；正确 |
| D2 | 穿搭 Agent 是先读取衣服还是先调用搭配模型？请依据已上传文档解释。 | rag_search | 多选 ['pr_review'] 漏选 ['rag_search']；多选 ['pr_review'] 漏选 ['rag_search'] | 正确；正确 |
| D3 | scene 参数是不是动态的？只查项目文档，不做 Issue、PR 或 CI 分析。 | rag_search | 多选 ['ci_debug', 'issue_analysis', 'pr_review'] 漏选 ['rag_search']；多选 ['ci_debug', 'issue_analysis', 'pr_review'] 漏选 ['rag_search'] | 正确；正确 |
| D4 | 根据项目说明解释会话 agent_run、SSE 恢复和 JDBC ChatMemory 的关系，仅检索知识库。 | rag_search | 正确；正确 | 正确（兜底）；正确 |
| G1 | 回顾默认形象删除的工程闭环，关联 Issue #1、PR #2 和 CI #37002998264。 | ci_debug, issue_analysis, pr_review | 正确；正确 | 正确（兜底）；正确 |
| G2 | 只汇总 wardrobeAgent 的仓库健康基线，不展开 Issue、PR、CI 或历史文档分析。 | 无 | 多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 []；多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 [] | 多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 []（兜底）；正确 |
| G3 | 回顾 PR #2 的改动及 CI #37002998264 的结果，不分析 Issue。 | ci_debug, pr_review | 多选 ['issue_analysis'] 漏选 []；多选 ['issue_analysis'] 漏选 [] | 正确；正确 |
| G4 | 回顾 Issue #1 与 PR #2 的对应关系，不检查 CI，也不检索历史。 | issue_analysis, pr_review | 多选 ['ci_debug', 'rag_search'] 漏选 []；多选 ['ci_debug', 'rag_search'] 漏选 [] | 正确；多选 ['ci_debug', 'rag_search'] 漏选 []（兜底） |

## 边界与复现

- Real synchronized repository evidence, but author-written questions and expected labels, not a production conversation sample.
- The same snapshot and real production target-resolution context are available to both planners; keyword rules ignore evidence text.
- Snapshot frozen before calls; original synthetic-suite prompt unchanged; expected labels never sent to the model.
- Only initial planner choice/graph/target binding is tested, not specialist execution, retrieval quality, replan or final answers.
- Closed Issue and merged PR are explicitly reviewed retrospectively, not presented as pending work.
- Snapshot may be stale; no GitHub sync or index repair was performed.
- Safety flags use production runtime logic; this suite has no external-write cases and cannot establish write-safety coverage.
- No retries; genuine failures are recorded separately as rule fallback. Mandatory gate repairs retained.
- Latency excludes semaphore queue time. Paid usage is provider-reported tokens; no unverified monetary estimate.

数据和逐次原始模型输出见同名 JSON；不含密钥和接口地址。正式 Planner 未切换。

在 backend 目录运行：

```powershell
.\.venv\Scripts\python.exe -m app.services.evals.project_planner_comparison --fixture ../docs/evals/wardrobe_planner_fixture.json --live --repeats 2 --concurrency 3
```
