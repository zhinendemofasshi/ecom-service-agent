# Agent 模型选型实验：GPT-4o mini vs GPT-5.6 Luna

> 这是 Prompt/评分口径修复前的同版本模型快照，用于说明当时的工具行为差异；
> 不应将 Luna 的 20% 与当前 gpt-4o-mini 的 96% 跨版本直接比较。若要做当前模型选型，
> 需要在同一代码、同一数据集 SHA 和同一时间窗口重新运行两种模型。

- 日期：2026-08-15
- 用例：同一份 50 条黄金集
- 模式：Multi-Agent optimized（高置信规则 + LLM 回退）
- 温度：`0.0`
- LLM-as-judge：关闭
- 数据集 SHA-256：`522d1e2520ba1c2982074275fb8cf7ed2d111cafcba13cc8ceca5a021247eeff`
- GPT-5.6 Luna 兼容参数：Chat Completions 使用 `reasoning_effort=none`

| 指标 | GPT-4o mini | GPT-5.6 Luna | Luna 相对变化 |
|---|---:|---:|---:|
| 严格综合通过率 | 62.0% | 20.0% | ↓ 42.0 pp |
| 平均结果得分 | 88.5% | 86.8% | ↓ 1.7 pp |
| 路由准确率 | 100.0% | 100.0% | 0.0 pp |
| 工具 Precision | 89.0% | 59.0% | ↓ 30.0 pp |
| 工具 Recall | 91.1% | 84.4% | ↓ 6.7 pp |
| 工具 F1 | 88.1% | 63.9% | ↓ 24.2 pp |
| LLM 调用次数 | 159 | 201 | ↑ 26.4% |
| 总 token | 195,330 | 314,342 | ↑ 60.9% |
| 估算 API 成本 | $0.037846 | $0.084281 | ↑ 122.7% |
| 平均端到端延迟 | 6,833 ms | 6,507 ms | ↓ 4.8% |
| P95 端到端延迟 | 12,455 ms | 11,685 ms | ↓ 6.2% |

成本分别按各模型本轮配置的输入/输出单价估算，仅包含 Agent chat completions，
不包含知识库 embedding 和关闭状态下的 evaluator judge。

## 结论

Luna 的平均结果得分只下降 1.7 pp，说明自然语言答案质量并没有随严格通过率同步
大幅下降；主要退化来自工具行为。Luna 更频繁调用 `load_skill`，并在订单、物流、
退款场景补查关联工具，导致工具 Precision 下降 30 pp、调用次数增加 26.4%。严格
通过判定要求所有已配置维度达到阈值，因此这些额外调用会让大量回答完整但工具不够
精简的用例失败。

在当前 Chat Completions + ReAct Prompt 下，GPT-4o mini 是更合适的默认模型：通过率、
工具 F1、token 和成本均明显占优。Luna 的延迟略低，但不足以抵消质量与成本退化。
如果继续使用 Luna，应先增加“仅在必要时加载 Skill、按用户问题调用最小工具集合”的
Prompt 约束后重新评估，而不是直接替换默认模型。

原始逐用例报告：

- [`optimized.json`](optimized.json)：GPT-4o mini
- [`luna-optimized.json`](luna-optimized.json)：GPT-5.6 Luna
