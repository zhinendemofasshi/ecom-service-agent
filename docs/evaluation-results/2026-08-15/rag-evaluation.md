# RAG 专项评估报告

评估时间：2026-08-15。数据集为 30 条人工标注问题，覆盖当前知识库全部 19 个
Chunk，并包含 3 条需要同时召回多个 Chunk 的复合问题。完整逐用例结果见
[`rag-evaluation.json`](rag-evaluation.json)，检索独立基线见
[`rag-retrieval-baseline.json`](rag-retrieval-baseline.json)。

## 指标口径

- `Recall@K`：Top-K 中召回的相关 Chunk 数 / 人工标注的全部相关 Chunk 数；因此
  多 Chunk 问题会出现 0.5，而不是简单的命中/未命中。
- `MRR`：首个相关 Chunk 排名的倒数均值。本轮在全部 19 个 Chunk 的完整排序上
  计算，不将 Top-5 之外的相关结果直接截断为 0。
- `Faithfulness`：回答中的每项事实是否都能由实际送入生成模型的 Top-5 上下文
  支持。遗漏由独立的关键事实覆盖率惩罚，避免“少答或空答”刷高忠实度。
- 关键事实覆盖率：人工标注的回答要点命中比例，支持人工维护的等价表述组。

## 最终真实结果

检索使用 `text-embedding-3-small + NumpyBackend`；回答模型为 `gpt-4o-mini`，
Faithfulness 裁判模型为 `gpt-5.6-luna`，两者均固定 temperature 0。数据集
SHA-256 为 `1eaf6c6c74f678cb606311d5e927e53fab2f077cfb0d518c0eb96458254016ea`。

| 指标 | 结果 |
|---|---:|
| Recall@1 | 85.0% |
| Recall@3 | 98.3% |
| Recall@5 | 100.0% |
| MRR | 0.9500 |
| Faithfulness | 100.0%（30/30） |
| 平均关键事实覆盖率 | 90.0% |
| 回答要点全覆盖率 | 73.3%（22/30） |
| 检索异常 / 评审异常 | 0 / 0 |
| 检索延迟 | avg 578 ms / P95 1,069 ms |
| 完整链路延迟 | avg 3,593 ms / P95 4,777 ms |
| Chat tokens | input 74,857 / output 3,936 |
| 已知回答生成成本 | $0.005907 |

检索延迟取不含生成和 Judge 的独立 30 条基线，完整链路延迟取最终全量评估。
成本只估算已配置公开单价的 `gpt-4o-mini` 回答生成；项目没有配置
`gpt-5.6-luna` Judge 单价，因此报告保留其真实 token，不编造总成本。Query
embedding 成本也不在该成本口径内。

分场景看，退换货类 Recall@1/MRR 均为 100%；FAQ 类 Recall@1 为 73.1%、
MRR 为 0.8846，是下一轮最值得增加 reranker 或混合检索的区域。尽管部分 FAQ
相关 Chunk 排到第 2～4 位，30 条用例在 Top-5 均完整召回，因此当前生成上下文没有
检索遗漏。

## Judge 校准前后

首轮用 `gpt-4o-mini` 同时生成回答和担任 Judge，Faithfulness 为 96.7%。唯一失败
样例的回答逐字复述了知识库中的“双倍积分”和“88 元无门槛券”，但 Judge 将不同
会员范围的两条可并存政策误判为冲突。保留的原始证据见
[`rag-evaluation-before-prompt.json`](rag-evaluation-before-prompt.json)。

随后明确“所有上下文构成事实并集、不同适用范围可并存”，并将 Judge 与被测回答
模型解耦，改由 `gpt-5.6-luna` 裁判。同一数据集完整重跑后，Faithfulness 从
96.7% 提升到 100%，Recall、MRR 和关键事实覆盖率不变。

| 指标 | 校准前 | 校准后 | 变化 |
|---|---:|---:|---:|
| Faithfulness | 96.7% | 100.0% | +3.3 pp |
| Recall@5 | 100.0% | 100.0% | 不变 |
| MRR | 0.9500 | 0.9500 | 不变 |
| 关键事实覆盖率 | 90.0% | 90.0% | 不变 |
| 平均端到端延迟 | 3,214 ms | 3,593 ms | +11.8% |
| 总 Chat tokens | 71,212 | 78,793 | +10.6% |

这次提升是评估器可靠性提升，不应包装成被测 RAG 回答质量从 96.7% 变好。代价是
Judge token 和延迟增加；30 条样本也不足以把 100% 外推为生产环境无幻觉。

## 复现

```bash
# 只跑检索层，不生成回答
python -m app.scripts.run_rag_eval --no-judge \
  --output docs/evaluation-results/2026-08-15/rag-retrieval-baseline.json

# 回答模型与 Judge 解耦的完整评估
python -m app.scripts.run_rag_eval --judge \
  --model gpt-4o-mini \
  --judge-model gpt-5.6-luna \
  --output docs/evaluation-results/2026-08-15/rag-evaluation.json
```

### 校招简历表述

> 构建覆盖全部 19 个知识 Chunk 的 30 条 RAG 专项评估集，引入 Recall@1/3/5、MRR、Faithfulness 与关键事实覆盖率，并将回答模型与 LLM Judge 解耦；真实评测 Recall@3/5 达 98.3%/100%、MRR 0.95、Faithfulness 100%，保留逐用例排名、token、延迟及成本口径以支持可复现回归。
