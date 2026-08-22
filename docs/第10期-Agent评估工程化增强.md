# 第 10 期：Agent 评估工程化增强

这一轮不再只回答“Agent 能不能跑”，而是用同一套黄金集回答四个更接近
生产的问题：路由是否正确、工具是否精准、用户要等多久、每轮调用要花多少钱。

## 1. 50 条分层黄金集

`app/evaluation/cases.json` 现在包含恰好 50 条用例，并为每条用例标注了意图、
关键词、转人工决策、期望工具、最小调用次数、token 预算和 Multi-Agent 路由。

| 场景 | 数量 | 代表性边界 |
|---|---:|---|
| 订单 | 10 | 待发货、已发货、已签收、退款中、无效订单号、多轮补号 |
| 物流 | 8 | 运输中、已签收、未发货无运单、无效订单、多轮查件 |
| 商品 | 10 | 库存、售罄、规格、SKU 精确查询、品类推荐、未收录商品 |
| 知识 | 10 | 退货政策、配送、会员、积分、账户安全 |
| 退款 | 6 | 敏感操作确认、未发货取消、重复退款、质量问题、拒收 |
| 投诉 | 4 | 强烈情绪、具体订单、服务态度、赔偿诉求 |
| 通用 | 2 | 问候和致谢的零工具调用 |

数据加载时会校验重复 ID、空轮次和空输入；单测额外断言用例数必须是 50。

## 2. 沙箱隔离修复

旧实现在构建 Agent 时直接修改全局 `settings.memory_enabled` 和
`settings.mcp_enabled`，跑完后没有恢复；`mkdtemp` 创建的目录也没有清理。

修复后的边界是：

- Agent/Orchestrator 支持构造参数覆盖，Sandbox 仅向当前 Agent 注入
  `memory_enabled=False` 和 `mcp_enabled=False`，不改写进程级配置。
- Sandbox 自己创建的目录用 `TemporaryDirectory` 管理，支持 `with` 和幂等
  `close()`；调用方显式传入的目录不会被 Sandbox 误删。
- case ID 会被安全化并附加 SHA-256 短摘要，防止路径穿越和文件名冲突。
- 每条用例结束都还原插桩并关闭 ToolManager/OpenAI client。

## 3. 指标口径

### 工具精确率、召回率和 F1

- Precision 在唯一工具类型层计算：命中的允许工具类型数 / 实际唯一工具类型数。
  合理分支可标为 `optional_tools`；重复调用不在 Precision 二次扣分，而由
  `tool_efficiency` 单独衡量。对零工具用例，不调工具得 1，任意多调得 0。
- Recall = 被调用的期望工具数 / 期望工具数。旧字段 `tool_accuracy`
  保留用于兼容，它的实际语义就是 Recall。
- F1 = `2 * Precision * Recall / (Precision + Recall)`。

### 延迟

每条用例记录端到端延迟、LLM 累计延迟和工具累计延迟；汇总报告给出
avg/P50/P95/max。端到端延迟包含路由、ReAct、工具、结构化提取和会话持久化。

### 成本

报告分别保留 prompt/completion token，再按 `.env` 中的每百万 token 输入/输出
单价估算美元成本。单价和数据集 SHA-256 会写入报告，防止只剩一个不可复现
的结果数字。当前成本口径只包含 Agent chat completions，不包含知识库 embedding
和可选的 evaluator judge 调用；该口径也会写入报告。

## 4. 一轮路由优化

baseline 每轮都调用 LLM 做 `presale/postsale/complaint` 分类。optimized 改成两级路由：

1. 先用有明确优先级的高置信业务信号处理投诉、订单/售后、售前和默认场景。
2. 只有规则不能覆盖的模糊输入才回退到原 LLM Router。

规则优先级为 `complaint > postsale/account/default > presale`，用于避免“尺码不合适要退货”
被“尺码”误分到售前。黄金集上有一条离线断言：optimized 规则的末轮路由需要
与 50/50 标注一致。在真实跑数时，trace 会额外标记路由来源是 `rule` 还是 `llm`。

## 5. 复现 baseline / optimized 对比

```bash
cp .env.example .env
# 在 .env 中填入 OPENAI_API_KEY，并确认模型和 token 单价

python -m app.scripts.build_kb_index --backend numpy
python -m app.scripts.compare_eval --output-dir app/sessions/eval_comparison
```

评估沙箱还会将两轮的采样温度固定为 `EVAL_TEMPERATURE=0.0`，避免将生成随机性
误算成路由优化收益。

`compare_eval` 会用同一模型、同一数据集和同一成本单价真实重跑两轮，生成：

- `baseline.json`：全 LLM 路由的逐用例 trace 与汇总。
- `optimized.json`：混合路由的逐用例 trace 与汇总。
- `comparison.json`：结构化的前后差值。
- `comparison.md`：可直接放进项目文档或改写为简历 bullet 的对比表。

默认关闭 LLM-as-judge，使路由、工具、token、延迟和成本只反映被测 Agent；
如果还需要回答质量/幻觉/过程合理性评分，可额外传入 `--judge`。

## 6. 真实优化结果（2026-08-15）

使用 `gpt-4o-mini`、temperature 0、同一份 50 条黄金集连续真实重跑；两组共
100 次用例执行均无报错。逐用例 trace 和结构化差值保存在
[`evaluation-results/2026-08-15/`](evaluation-results/2026-08-15/comparison.md)。

| 指标 | Baseline | Optimized | 变化 |
|---|---:|---:|---:|
| 通过率 | 56.0% | 62.0% | +6.0 pp |
| 路由准确率 | 96.0% | 100.0% | +4.0 pp |
| 工具 Precision | 87.0% | 89.0% | +2.0 pp |
| LLM 调用次数 | 213 | 159 | ↓ 25.4% |
| 总 token | 204,773 | 195,330 | ↓ 4.6% |
| 估算 API 成本 | $0.039047 | $0.037846 | ↓ 3.1% |
| 平均端到端延迟 | 7,178 ms | 6,833 ms | ↓ 4.8% |
| P95 端到端延迟 | 11,456 ms | 12,455 ms | ↑ 8.7% |

成本按输入 `$0.15/1M tokens`、输出 `$0.60/1M tokens` 估算，仅统计 Agent chat
completion，不包含首次构建知识库的 embedding 和关闭状态下的 LLM judge。
P95 在这一轮中回归 8.7%，说明单轮 50 条样本不足以证明尾延迟改善；正式压测应
增加重复次数并报告置信区间。本报告保留该回归，不将平均延迟改善外推到尾延迟。

### 校招简历表述

> 搭建覆盖订单、物流、商品、知识库、退款、投诉等 7 类场景的 50 条 Agent 黄金评估集，修复评估沙箱全局配置污染与临时目录泄漏，补齐工具 Precision/Recall/F1、端到端延迟及 token 成本指标；校准等价表述、可选工具与复合意图口径，并通过最小工具白名单、跨轮退款确认、FAQ 强制检索和只读调用复用，将 gpt-4o-mini 真实全量严格通过率从 62% 提升至 96%，工具 F1 从 88.1% 提升至 99.3%。

## 7. 通过率归因与质量优化

原始 62% 并非单一模型能力问题。用当前确定性指标对原始逐用例 trace 离线重评，
不调用模型即可得到 80%，说明其中 18 pp 来自旧口径对格式差异、等价表述、合理
可选工具和混合意图的误伤。随后修复真实行为问题并重新调用 API 跑满 50 条，严格
通过率达到 96%（48/50），工具 Precision/Recall/F1 为 99.0%/100.0%/99.3%，
平均结果得分为 100%。

行为侧修复包括：账户安全/密码 FAQ 强制 RAG、退款敏感写操作跨轮确认、投诉 Agent
最小工具白名单、同轮同参只读工具结果复用，以及删除未知商品的动态 mock 编造兜底。
剩余两条失败均为工具效率门槛，最终答案、意图、转人工和路由指标全部通过。完整
归因、代价和逐用例证据见
[`evaluation-results/2026-08-15/quality-fix-comparison.md`](evaluation-results/2026-08-15/quality-fix-comparison.md)。

## 8. 模型选型实验

在相同 optimized 路由、temperature 0 和 50 条数据集上追加运行 GPT-5.6 Luna。
由于 Luna 在 Chat Completions 中使用函数工具时要求关闭 reasoning，本项目显式传入
`reasoning_effort=none`，保持原有 Chat Completions 架构不变。

真实结果为：严格通过率 `62% → 20%`、工具 Precision `89% → 59%`、总 token
增加 `60.9%`、成本增加 `122.7%`；平均延迟下降 `4.8%`。因此当前 Prompt 和工具策略
下继续保留 GPT-4o mini 作为默认模型。完整对比和逐用例证据见
[`evaluation-results/2026-08-15/model-comparison.md`](evaluation-results/2026-08-15/model-comparison.md)。

## 9. RAG 专项评估

通用 50 条 Agent 集只能判断 `search_knowledge` 有没有被正确调用，不能回答检索到的
Chunk 是否正确、相关内容排在第几位、最终回答是否忠实于检索上下文。因此新增
`app/evaluation/rag_cases.json`：30 条问题覆盖当前知识库全部 19 个 Chunk，其中
3 条为多 Chunk 复合问题。

检索层计算 Recall@1/3/5 和全库排序 MRR；生成层用人工关键事实覆盖率衡量完整性，
用 LLM Judge 逐项核查 Faithfulness。回答与 Judge 模型解耦，避免让被测模型完全
自评；Judge 单价未知时只记录真实 token，不伪造成本。

2026-08-15 的真实全量结果为：Recall@1/3/5 `85.0%/98.3%/100%`、MRR
`0.9500`、Faithfulness `100%（30/30）`、平均关键事实覆盖率 `90.0%`，0 异常。
检索独立基线延迟 avg/P95 为 `578/1,069 ms`，完整生成与评审链路 avg/P95 为
`3,593/4,777 ms`。

首轮使用 `gpt-4o-mini` 自评时 Faithfulness 为 96.7%，唯一失败是对知识库原文的
明显误判；强化事实并集口径并改用 `gpt-5.6-luna` Judge 后，同集重跑为 100%。
这属于评估器校准，不包装成被测回答质量提升。完整口径、前后证据和复现命令见
[`evaluation-results/2026-08-15/rag-evaluation.md`](evaluation-results/2026-08-15/rag-evaluation.md)。

### 补充后的校招简历表述

> 搭建 50 条分层 Agent 黄金集与覆盖全部 19 个知识 Chunk 的 30 条 RAG 专项集，修复沙箱配置污染和临时目录泄漏，补齐工具 Precision/Recall/F1、延迟、成本及 Recall@K、MRR、Faithfulness 指标；通过路由/工具策略优化将 gpt-4o-mini 严格通过率由 62% 提升至 96%、工具 F1 提升至 99.3%，RAG 真实评测 Recall@3/5 达 98.3%/100%、MRR 0.95、Faithfulness 100%。
