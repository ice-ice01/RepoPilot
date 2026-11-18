# Planner 对照实验

模型：gpt-6.1-sol；用例：32；每例重复：2。

只测首次规划中的 Agent 选择和任务图，不执行专用 Agent，不代表最终回答质量或重规划效果。

## 结论与解读

在这组预先标注的合成诊断题中，混合方案选择更准确，但明显更慢：完全正确 62/64 次，现有规则 30/64 次。模型规划中位耗时 7.32 秒，P95 为 29.94 秒；现有规则中位耗时约 0.04 毫秒。两轮并非 64 个独立问题，而是同一组 32 个问题的重复测试。

- 本次 64 次模型请求均返回可解析、可校验的计划，没有调用失败或规则回退；不能据此断言真实环境永不失败。离线测试另外验证了异常、非法计划、环依赖、越权字段的回退行为，相关测试共 28 项通过。
- 有两次模型省略仓库基线，程序硬约束补回。因此 62/64 是“模型＋程序约束”的成绩，不是裸模型成绩。
- 第二轮 S4 将 `enable_rag_task=false` 错当成“禁止检索”，漏选 rag_search。当前语义实际是“不强制检索”，用户主动要求时仍应选中。后续应把“可用能力”和“强制任务”分开传递，减少歧义。
- 第二轮 S1 为改动审查额外加入 CI 分析，按预先冻结的“仅审查改动”标签计为多选。不过上下文确有失败 CI，这个额外检查也有业务合理性；是否应该算错取决于产品的任务范围与成本策略。主结果没有事后修改标签。
- 这两次偏差的输出都合法，因此**规则兜底不会自动修复语义错误**。仍需要清晰的任务范围、Observer 检查，以及针对真实问题的回归测试。
- 强制安全用例为 4 题 × 2 轮，8/8 通过；安全标志由测试直接提供，并未验证上游是否能正确识别所有写入意图。

建议：如果可以接受额外规划延迟，优先考虑模型规划、程序硬约束、规则故障兜底的组合。正式切换前，应增加真实用户问题的独立标注测试、重规划测试和端到端效果评估。本次没有切换正式 Planner，没有执行业务 Agent，也没有为提高分数在运行后调整提示词或预期标签。

本次实验原型由模型决定任务类型、目标与依赖；Agent 身份、目标 ID、证据权限和验收标准仍由程序绑定。这不是让模型任意生成执行权限或调用任意工具。

| 指标 | 现有规则 | 模型＋校验＋规则兜底 |
|---|---:|---:|
| Agent 集合完全正确 | 46.9% | 96.9% |
| 专用任务精确率 | 59.7% | 98.8% |
| 专用任务召回率 | 86.0% | 98.8% |
| F1 | 70.5% | 98.8% |
| 多选任务总数 | 50 | 1 |
| 漏选任务总数 | 12 | 1 |
| 计划结构/对象合法率 | 100.0% | 100.0% |
| 强制安全门通过率 | 100.0% | 100.0% |
| 耗时中位数 ms | 0.04 | 7321.91 |
| 耗时 P95 ms | 0.12 | 29938.95 |
| 规则兜底次数 | 0 | 0 |

## 分类正确率

| 类别 | 规则 | 混合方案 |
|---|---:|---:|
| 明确指令 | 8/8 | 8/8 |
| 同义表达 | 2/8 | 6/8 |
| 否定与限定 | 0/8 | 8/8 |
| 多目标 | 4/8 | 8/8 |
| 缺少目标 | 2/8 | 8/8 |
| 全局评估 | 6/8 | 8/8 |
| 边界与误触发 | 0/8 | 8/8 |
| 安全门 | 8/8 | 8/8 |

模型选择集合重复一致：30/32（至少两轮才有意义）。
接口报告总 token：153922；不估算未核实单价的费用。
程序补入强制任务次数：2。

## 每例结果（不同轮次保留全部记录）

| 用例 | 用户问题 | 预期专用任务 | 规则 | 混合方案 |
|---|---|---|---|---|
| E1 | 分析 Issue #142 的优先级和负责人。 | issue_analysis | 正确；正确 | 正确；正确 |
| E2 | 请 review PR #86 的 diff。 | pr_review | 正确；正确 | 正确；正确 |
| E3 | 定位 CI #9001 的构建失败原因。 | ci_debug | 正确；正确 | 正确；正确 |
| E4 | 检索知识库中的历史处理记录。 | rag_search | 正确；正确 | 正确；正确 |
| S1 | 帮我看看这次提交的改动有没有引入隐患。 | pr_review | 正确；正确 | 正确；多选 ['ci_debug'] 漏选 [] |
| S2 | 自动检查一直亮红灯，帮我找出原因。 | ci_debug | 多选 ['pr_review'] 漏选 ['ci_debug']；多选 ['pr_review'] 漏选 ['ci_debug'] | 正确；正确 |
| S3 | 这张工单该交给谁跟进？ | issue_analysis | 多选 ['pr_review'] 漏选 ['issue_analysis']；多选 ['pr_review'] 漏选 ['issue_analysis'] | 正确；正确 |
| S4 | 以前有没有遇到过这种现象，当时是怎么解决的？ | rag_search | 多选 ['pr_review'] 漏选 ['rag_search']；多选 ['pr_review'] 漏选 ['rag_search'] | 正确；多选 [] 漏选 ['rag_search'] |
| N1 | 只审查 PR，不要分析 Issue、CI，也不要查历史。 | pr_review | 多选 ['ci_debug', 'issue_analysis', 'rag_search'] 漏选 []；多选 ['ci_debug', 'issue_analysis', 'rag_search'] 漏选 [] | 正确；正确 |
| N2 | 只分析 CI 日志，不审查 PR，也不处理 Issue。 | ci_debug | 多选 ['issue_analysis', 'pr_review'] 漏选 []；多选 ['issue_analysis', 'pr_review'] 漏选 [] | 正确；正确 |
| N3 | 只查历史案例，不做 Issue、PR 或 CI 分析。 | rag_search | 多选 ['ci_debug', 'issue_analysis', 'pr_review'] 漏选 []；多选 ['ci_debug', 'issue_analysis', 'pr_review'] 漏选 [] | 正确；正确 |
| N4 | 只分析 Issue 的负责人，不要审查 PR 或检查 CI。 | issue_analysis | 多选 ['ci_debug', 'pr_review'] 漏选 []；多选 ['ci_debug', 'pr_review'] 漏选 [] | 正确；正确 |
| M1 | 这个改动现在能上线吗？请核查改动和自动检查结果。 | ci_debug, pr_review | 多选 [] 漏选 ['ci_debug']；多选 [] 漏选 ['ci_debug'] | 正确；正确 |
| M2 | 分析 Issue #142，并查找历史解决方案。 | issue_analysis, rag_search | 正确；正确 | 正确；正确 |
| M3 | 审查 PR #86，并检索相似修改的处理记录。 | pr_review, rag_search | 正确；正确 | 正确；正确 |
| M4 | 定位 CI 失败并审查 PR 改动，不分析 Issue。 | ci_debug, pr_review | 多选 ['issue_analysis'] 漏选 []；多选 ['issue_analysis'] 漏选 [] | 正确；正确 |
| A1 | 请 review PR 的 diff。 | 无 | 多选 ['ci_debug'] 漏选 []；多选 ['ci_debug'] 漏选 [] | 正确；正确 |
| A2 | 请定位 CI 构建失败原因。 | 无 | 多选 ['pr_review'] 漏选 []；多选 ['pr_review'] 漏选 [] | 正确；正确 |
| A3 | 分析 Issue 的优先级。 | 无 | 多选 ['pr_review'] 漏选 []；多选 ['pr_review'] 漏选 [] | 正确；正确 |
| A4 | 全面评估当前仓库工程状态。 | 无 | 正确；正确 | 正确；正确 |
| B1 | 全面评估当前仓库工程状态，覆盖工单、代码改动和自动检查。 | ci_debug, issue_analysis, pr_review | 正确；正确 | 正确；正确 |
| B2 | 请多 Agent 协作，检查 Issue、PR 和 CI。 | ci_debug, issue_analysis, pr_review | 正确；正确 | 正确；正确 |
| B3 | 从整体识别仓库阻塞，分析工单、代码改动和自动检查。 | ci_debug, issue_analysis, pr_review | 正确；正确 | 正确；正确 |
| B4 | 把所有工单、待审查的改动、自动检查运行一起评估一下。 | ci_debug, issue_analysis, pr_review | 多选 [] 漏选 ['ci_debug', 'issue_analysis']；多选 [] 漏选 ['ci_debug', 'issue_analysis'] | 正确；正确 |
| K1 | 只给仓库健康概览，不展开 Issue、PR、CI 或历史分析。 | 无 | 多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 []；多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 [] | 正确；正确 |
| K2 | 只在知识库查 pricing 文档，不分析工程对象。 | rag_search | 多选 ['ci_debug', 'pr_review'] 漏选 []；多选 ['ci_debug', 'pr_review'] 漏选 [] | 正确；正确 |
| K3 | 解释 action 这个术语，只查知识库，不分析工程对象。 | rag_search | 多选 ['ci_debug'] 漏选 []；多选 ['ci_debug'] 漏选 [] | 正确；正确 |
| K4 | 说明 priority 字段含义，只检索知识库，不分析工程对象。 | rag_search | 多选 ['pr_review'] 漏选 []；多选 ['pr_review'] 漏选 [] | 正确；正确 |
| W1 | 审查 PR 和 CI，并生成合并操作草稿，等待我确认。 | ci_debug, pr_review, safety_review | 正确；正确 | 正确；正确 |
| W2 | 请把通知发到群里，只生成发送草稿等我确认。 | safety_review | 正确；正确 | 正确；正确 |
| W3 | 分析 Issue 后生成分配操作草稿，等待确认。 | issue_analysis, safety_review | 正确；正确 | 正确；正确 |
| W4 | 检查 CI 后生成重新运行的操作草稿，等待确认。 | ci_debug, safety_review | 正确；正确 | 正确；正确 |

## 边界与复现

- Synthetic, author-labelled diagnostic dataset; not production traffic.
- Expected labels frozen before live calls and hidden from the model.
- Hard safety flags supplied by fixtures; upstream detection is not tested.
- Mandatory baseline/safety/RAG gates can repair omissions; raw decisions and repairs retained.
- No prompt tuning after seeing live labels; no model retry; failures count as rule fallback.
- Latency includes HTTP setup/provider processing but excludes semaphore queue time.
- Token usage reported by provider; no monetary estimate without a verified price.
- Stability only meaningful with at least two repeats; sampling follows production API defaults.

数据和逐次原始模型输出见同名 JSON；不含密钥和接口地址。正式 Planner 未切换。

在 backend 目录运行：

```powershell
.\.venv\Scripts\python.exe -m app.services.evals.planner_comparison --live --repeats 2 --concurrency 3
```
