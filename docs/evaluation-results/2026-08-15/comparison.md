# Agent 评估优化实验

- 模型：`gpt-4o-mini`
- 用例：50 条
- 模式：Multi-Agent，baseline 全 LLM 路由 vs optimized 高置信规则 + LLM 回退
- 采样温度：`0.0`
- 数据集 SHA-256：`522d1e2520ba1c2982074275fb8cf7ed2d111cafcba13cc8ceca5a021247eeff`
- 成本口径：agent chat completions only; embeddings and evaluator judge excluded

| 指标 | Baseline | Optimized | 变化 |
|---|---:|---:|---:|
| 通过率 | 56.0% | 62.0% | +6.0 pp |
| 路由准确率 | 96.0% | 100.0% | +4.0 pp |
| 工具 Precision | 87.0% | 89.0% | +2.0 pp |
| LLM 调用次数 | 213 | 159 | ↓ 25.4% |
| 总 token | 204773 | 195330 | ↓ 4.6% |
| 估算 API 成本 | $0.039047 | $0.037846 | ↓ 3.1% |
| 平均延迟 | 7178 ms | 6833 ms | ↓ 4.8% |
| P95 延迟 | 11456 ms | 12455 ms | ↑ 8.7% |

> 以上数值由 `app/scripts/compare_eval.py` 对同一黄金集连续真实重跑生成；原始逐用例 trace 见同目录 JSON 报告。
