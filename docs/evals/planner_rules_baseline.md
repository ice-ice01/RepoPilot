# Planner 对照实验

模型：未调用；用例：32；每例重复：1。

只测首次规划中的 Agent 选择和任务图，不执行专用 Agent，不代表最终回答质量或重规划效果。

| 指标 | 现有规则 | 模型＋校验＋规则兜底 |
|---|---:|---:|
| Agent 集合完全正确 | 46.9% | 未测 |
| 专用任务精确率 | 59.7% | 未测 |
| 专用任务召回率 | 86.0% | 未测 |
| F1 | 70.5% | 未测 |
| 多选任务总数 | 25 | 未测 |
| 漏选任务总数 | 6 | 未测 |
| 计划结构/对象合法率 | 100.0% | 未测 |
| 强制安全门通过率 | 100.0% | 未测 |
| 耗时中位数 ms | 0.04 | 未测 |
| 耗时 P95 ms | 0.16 | 未测 |
| 规则兜底次数 | 0 | 未测 |

## 分类正确率

| 类别 | 规则 | 混合方案 |
|---|---:|---:|
| 明确指令 | 4/4 | 未测 |
| 同义表达 | 1/4 | 未测 |
| 否定与限定 | 0/4 | 未测 |
| 多目标 | 2/4 | 未测 |
| 缺少目标 | 1/4 | 未测 |
| 全局评估 | 3/4 | 未测 |
| 边界与误触发 | 0/4 | 未测 |
| 安全门 | 4/4 | 未测 |

模型选择集合重复一致：0/32（至少两轮才有意义）。
接口报告总 token：0；不估算未核实单价的费用。
程序补入强制任务次数：0。

## 每例结果（不同轮次保留全部记录）

| 用例 | 用户问题 | 预期专用任务 | 规则 | 混合方案 |
|---|---|---|---|---|
| E1 | 分析 Issue #142 的优先级和负责人。 | issue_analysis | 正确 | 未测 |
| E2 | 请 review PR #86 的 diff。 | pr_review | 正确 | 未测 |
| E3 | 定位 CI #9001 的构建失败原因。 | ci_debug | 正确 | 未测 |
| E4 | 检索知识库中的历史处理记录。 | rag_search | 正确 | 未测 |
| S1 | 帮我看看这次提交的改动有没有引入隐患。 | pr_review | 正确 | 未测 |
| S2 | 自动检查一直亮红灯，帮我找出原因。 | ci_debug | 多选 ['pr_review'] 漏选 ['ci_debug'] | 未测 |
| S3 | 这张工单该交给谁跟进？ | issue_analysis | 多选 ['pr_review'] 漏选 ['issue_analysis'] | 未测 |
| S4 | 以前有没有遇到过这种现象，当时是怎么解决的？ | rag_search | 多选 ['pr_review'] 漏选 ['rag_search'] | 未测 |
| N1 | 只审查 PR，不要分析 Issue、CI，也不要查历史。 | pr_review | 多选 ['ci_debug', 'issue_analysis', 'rag_search'] 漏选 [] | 未测 |
| N2 | 只分析 CI 日志，不审查 PR，也不处理 Issue。 | ci_debug | 多选 ['issue_analysis', 'pr_review'] 漏选 [] | 未测 |
| N3 | 只查历史案例，不做 Issue、PR 或 CI 分析。 | rag_search | 多选 ['ci_debug', 'issue_analysis', 'pr_review'] 漏选 [] | 未测 |
| N4 | 只分析 Issue 的负责人，不要审查 PR 或检查 CI。 | issue_analysis | 多选 ['ci_debug', 'pr_review'] 漏选 [] | 未测 |
| M1 | 这个改动现在能上线吗？请核查改动和自动检查结果。 | ci_debug, pr_review | 多选 [] 漏选 ['ci_debug'] | 未测 |
| M2 | 分析 Issue #142，并查找历史解决方案。 | issue_analysis, rag_search | 正确 | 未测 |
| M3 | 审查 PR #86，并检索相似修改的处理记录。 | pr_review, rag_search | 正确 | 未测 |
| M4 | 定位 CI 失败并审查 PR 改动，不分析 Issue。 | ci_debug, pr_review | 多选 ['issue_analysis'] 漏选 [] | 未测 |
| A1 | 请 review PR 的 diff。 | 无 | 多选 ['ci_debug'] 漏选 [] | 未测 |
| A2 | 请定位 CI 构建失败原因。 | 无 | 多选 ['pr_review'] 漏选 [] | 未测 |
| A3 | 分析 Issue 的优先级。 | 无 | 多选 ['pr_review'] 漏选 [] | 未测 |
| A4 | 全面评估当前仓库工程状态。 | 无 | 正确 | 未测 |
| B1 | 全面评估当前仓库工程状态，覆盖工单、代码改动和自动检查。 | ci_debug, issue_analysis, pr_review | 正确 | 未测 |
| B2 | 请多 Agent 协作，检查 Issue、PR 和 CI。 | ci_debug, issue_analysis, pr_review | 正确 | 未测 |
| B3 | 从整体识别仓库阻塞，分析工单、代码改动和自动检查。 | ci_debug, issue_analysis, pr_review | 正确 | 未测 |
| B4 | 把所有工单、待审查的改动、自动检查运行一起评估一下。 | ci_debug, issue_analysis, pr_review | 多选 [] 漏选 ['ci_debug', 'issue_analysis'] | 未测 |
| K1 | 只给仓库健康概览，不展开 Issue、PR、CI 或历史分析。 | 无 | 多选 ['ci_debug', 'issue_analysis', 'pr_review', 'rag_search'] 漏选 [] | 未测 |
| K2 | 只在知识库查 pricing 文档，不分析工程对象。 | rag_search | 多选 ['ci_debug', 'pr_review'] 漏选 [] | 未测 |
| K3 | 解释 action 这个术语，只查知识库，不分析工程对象。 | rag_search | 多选 ['ci_debug'] 漏选 [] | 未测 |
| K4 | 说明 priority 字段含义，只检索知识库，不分析工程对象。 | rag_search | 多选 ['pr_review'] 漏选 [] | 未测 |
| W1 | 审查 PR 和 CI，并生成合并操作草稿，等待我确认。 | ci_debug, pr_review, safety_review | 正确 | 未测 |
| W2 | 请把通知发到群里，只生成发送草稿等我确认。 | safety_review | 正确 | 未测 |
| W3 | 分析 Issue 后生成分配操作草稿，等待确认。 | issue_analysis, safety_review | 正确 | 未测 |
| W4 | 检查 CI 后生成重新运行的操作草稿，等待确认。 | ci_debug, safety_review | 正确 | 未测 |

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
