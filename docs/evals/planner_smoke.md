# Planner 对照实验

模型：gpt-6.1-sol；用例：1；每例重复：1。

只测首次规划中的 Agent 选择和任务图，不执行专用 Agent，不代表最终回答质量或重规划效果。

| 指标 | 现有规则 | 模型＋校验＋规则兜底 |
|---|---:|---:|
| Agent 集合完全正确 | 100.0% | 100.0% |
| 专用任务精确率 | 100.0% | 100.0% |
| 专用任务召回率 | 100.0% | 100.0% |
| F1 | 100.0% | 100.0% |
| 多选任务总数 | 0 | 0 |
| 漏选任务总数 | 0 | 0 |
| 计划结构/对象合法率 | 100.0% | 100.0% |
| 强制安全门通过率 | 100.0% | 100.0% |
| 耗时中位数 ms | 1.35 | 21774.77 |
| 耗时 P95 ms | 1.35 | 21774.77 |
| 规则兜底次数 | 0 | 0 |

## 分类正确率

| 类别 | 规则 | 混合方案 |
|---|---:|---:|
| 明确指令 | 1/1 | 1/1 |

模型选择集合重复一致：1/1（至少两轮才有意义）。
接口报告总 token：1793；不估算未核实单价的费用。
程序补入强制任务次数：0。

## 每例结果（不同轮次保留全部记录）

| 用例 | 用户问题 | 预期专用任务 | 规则 | 混合方案 |
|---|---|---|---|---|
| E1 | 分析 Issue #142 的优先级和负责人。 | issue_analysis | 正确 | 正确 |

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
