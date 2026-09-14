# Ecom-Service-Agent: 从0到1实战企业级电商客服Agent系统

## 快速开始（Quick Start）

跑起来只需要一个 OpenAI API Key，5 分钟即可看到「小夕」上线对话。

```bash
# 1. 进入项目并创建虚拟环境（Python 3.11+）
cd ecom-service-agent
python3.11 -m venv .venv && source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API Key
cp .env.example .env
# 编辑 .env，至少填入：
#   OPENAI_API_KEY=sk-你的key
#   （可选）OPENAI_BASE_URL=https://... 如用中转/代理
#   （可选）MODEL_NAME=gpt-4o-mini

# 4. 构建知识库索引（RAG 检索需要，首次运行一次即可）
python -m app.scripts.build_kb_index

# 5. 启动对话
python main.py
```

启动后直接输入问题即可，试试这些：

- `我的订单还没发货，怎么回事？` —— 触发订单 + 物流查询
- `有没有宽松透气的裤子推荐？` —— 触发商品推荐技能
- `这件衣服质量有问题，我要退货` —— 触发退货退款流程

每条回复底部会显示 `[意图 | 置信度 | 是否转人工]`。对话中还支持这些命令：

| 命令            | 作用                 |
| --------------- | -------------------- |
| `skills`        | 查看已加载的技能模块 |
| `memory`        | 查看短期 / 长期记忆  |
| `reset`         | 清空当前会话         |
| `quit` / `exit` | 退出                 |

**开启进阶能力**（可选，改 `.env` 后重启即可）：

- `AGENT_RUNTIME=langgraph` —— 默认使用 LangGraph + Responses API；设置为 `legacy` 可回退旧 ReAct 循环
- `HITL_ENABLED=true` —— `apply_refund` 执行前持久化暂停，输入 `approve` / `reject` 恢复
- `MULTI_AGENT_ENABLED=true` —— 多 Agent 协作（售前/售后/投诉分流）
- `MCP_ENABLED=true` —— 通过 MCP 协议调用工具（需另起 `python mcp_server/server.py`）
- `RAG_BACKEND=chroma` —— 换用 Chroma 向量数据库（需 `pip install chromadb`）

LangGraph checkpoint 默认写入 `app/sessions/langgraph-checkpoints.sqlite`，用于在进程重启后恢复待审核操作。SQLite 适合本地开发；`CHECKPOINT_BACKEND=postgres` 现已统一启用 PostgreSQL checkpoint、API/事件、退款审批、会话及记忆存储。开启 `MULTI_AGENT_ENABLED` 后，Router、售前、售后、投诉均在同一个 LangGraph 中执行并共享 checkpoint。P1 实现、审批用法与边界见 [P1 验收说明](docs/p1-audit.md)。

### API、SSE 与生产配置

配置 `AUTH_TOKENS`（服务端签发/配置的高熵登录凭证）和 `REFUND_APPROVER_IDS` 后启动。PostgreSQL 模式支持多个 worker，SQLite 模式仍使用单进程：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000 --workers 2
```

默认携带 `Authorization: Bearer <token>`。`AUTH_TOKENS` 是 JSON 对象，键为高熵 token，值包含 `tenant_id`、`user_id`、`actor_id`、`roles` 和 Unix 秒数 `expires_at`。身份从服务端映射读取，忽略客户端身份 header；过期凭证返回 401。审批人须同时有 `approver` 角色并列入 `REFUND_APPROVER_IDS`；管理员使用 `admin` 角色。客服审批 token 的 `user_id` 是被授权服务的订单用户，`actor_id` 是实际审批人。凭证由可信登录系统或管理员配置，本项目不提供密码登录或外部 IdP。

只有处在可信网关后时才设置 `TRUSTED_IDENTITY_HEADERS=true`，启用原有 `X-API-Key` / `X-Tenant-ID` / `X-User-ID` / `X-Actor-ID`，审批使用 `X-Approval-Key`，管理使用 `X-Admin-Key`。网关必须覆盖客户端提交的身份头；默认关闭此模式。

主要接口：

| 接口                                                                | 用途                                                      |
| ------------------------------------------------------------------- | --------------------------------------------------------- |
| `POST /sessions`                                                    | 创建隔离 session                                          |
| `POST /sessions/{id}/messages`                                      | 发消息并启动 Agent run                                    |
| `GET /sessions/{id}/events?after=0`                                 | SSE 读取 token、工具和审批事件；`after` 支持断线续传      |
| `POST /sessions/{id}/approval`                                      | `approve` / `edit` / `reject` 敏感操作                    |
| `POST /runs/{request_id}/approve`、`POST /runs/{request_id}/reject` | 按 run 审批；重复或过时审批返回 409                       |
| `POST /runs/{request_id}/cancel`                                    | 取消待审批操作不调用模型；执行中的 run 在下一个检查点停止 |
| `GET /runs/{request_id}`                                            | 查询运行、审批等待、完成、失败或取消状态                  |
| `POST /admin/checkpoints/cleanup`                                   | 清理超过保留期且已完成的 checkpoint                       |
| `GET /health`、`GET /ready`                                         | 存活和依赖就绪检查                                        |

PostgreSQL 模式使用统一共享存储和数据库执行锁。迁移旧数据、生命周期清理和集成测试说明见 [PostgreSQL 运行与迁移](docs/postgres.md)：

```dotenv
CHECKPOINT_BACKEND=postgres
DATABASE_URL=postgresql://user:password@postgres:5432/ecom_agent?sslmode=require
```

Responses API 的文本 delta 同时支持 CLI 实时打印与 SSE 持久化事件。消息 POST 会等待完成，但可以同时连接 events 接口读 token；建议预先设置 `X-Request-ID`，断线后查询对应 run，避免重复提交。POST 连接中断不会终止后台运行。SSE 接受 `Last-Event-ID` 或优先的 `after` 参数，过期游标返回 410，非法游标返回 422。`app.api_client.session_events` 提供自动重连、退避和游标去重；调用方持久化已处理事件的 `sequence`，服务保留最近 2000 个事件。

工具统一经过超时、只读暂态错误指数退避重试、熔断及全进程 32 个执行槽位；参数错误不重试，写操作不自动重试。线程超时不能撤销已经发出的写操作，因此超时任务实际退出前仍占用槽位，写操作结果需用业务幂等键核对。退款服务独立保存审批状态，验证订单归属、审批人白名单、审批有效期、金额/次数和幂等键，在事务内记录退款及审计。取消不需要再次调用模型；审批有效期在决定和实际执行时均核验，不依赖 Graph 的暂停逻辑。

当前订单来源仍是 `mock_data.ORDERS`，退款表示本地事务提交申请，没有连接支付渠道。Docker 和真实订单/支付系统部署验收见 [P0 核查记录](docs/p0-audit.md)。

**跑评估 & 测试**：

```bash
python -m app.scripts.run_eval --mode multi --variant optimized --no-judge
python -m app.scripts.run_eval --case-id knowledge_account_stolen --no-judge
python -m app.scripts.compare_eval    # 真实重跑 baseline/optimized 并生成 JSON + Markdown
python -m app.scripts.rescore_eval docs/evaluation-results/2026-08-15/optimized.json
python -m app.scripts.run_rag_eval --no-judge  # 30 条 Recall@1/3/5 + MRR
python -m app.scripts.run_rag_eval --judge --judge-model gpt-5.6-luna  # + Faithfulness
pytest                                # 运行全部单元测试
```

黄金集已扩展为 50 条，覆盖订单、物流、商品、知识库、退款、投诉和
通用对话。对比报告同时给出通过率、路由准确率、工具 Precision/Recall/F1、
输入/输出 token、估算 API 成本、平均/P50/P95 延迟。详细设计和复现口径见
[`docs/第10期-Agent评估工程化增强.md`](docs/第10期-Agent评估工程化增强.md)，
2026-08-15 的 50 条真实 A/B 结果见
[`docs/evaluation-results/2026-08-15/comparison.md`](docs/evaluation-results/2026-08-15/comparison.md)，
通过率误差归因与质量优化（62% → 96%）见
[`docs/evaluation-results/2026-08-15/quality-fix-comparison.md`](docs/evaluation-results/2026-08-15/quality-fix-comparison.md)，
GPT-4o mini 与 GPT-5.6 Luna 的同集模型选型实验见
[`docs/evaluation-results/2026-08-15/model-comparison.md`](docs/evaluation-results/2026-08-15/model-comparison.md)。
覆盖全部 19 个知识 Chunk 的 30 条 RAG 专项评估结果（Recall@3/5
98.3%/100%、MRR 0.95、Faithfulness 100%）见
[`docs/evaluation-results/2026-08-15/rag-evaluation.md`](docs/evaluation-results/2026-08-15/rag-evaluation.md)。

### 当前架构

```
ecom-service-agent/
├── main.py                        # CLI 入口（支持单 Agent / Multi-Agent 模式切换 + memory/skills 命令）
├── requirements.txt
├── .env.example
│
├── app/                           # 主 Bot 全部代码 + 数据
│   ├── config/
│   │   └── settings.py            # 配置管理（从 .env 读取，含 MCP / RAG / Multi-Agent / Memory / Skill / Evaluation 配置）
│   ├── prompts/
│   │   ├── customer_service.py    # 电商客服 system prompt（含工具使用指南 + 记忆能力）
│   │   ├── summarizer.py          # 历史摘要 prompt
│   │   ├── agents.py              # Multi-Agent 子 Agent prompt（售前/售后/投诉 + Router）
│   │   ├── memory.py              # 记忆提取 prompt（短期 STM / 长期 LTM 事实抽取）
│   │   └── evaluation.py          # LLM-as-judge prompt（回答质量 / 幻觉 / 过程合理性）
│   ├── schemas/
│   │   └── response.py            # 结构化输出 schema（Pydantic）
│   ├── agent/                     # Agent 核心实现 + 全部 Agent 技术栈（tools / rag / skills）
│   │   ├── chat.py                # 核心 ReAct 循环（集成 MemoryManager + SkillManager）
│   │   ├── summarizer.py          # LLM 自我压缩老对话（支持工具消息）
│   │   ├── storage.py             # 旧会话归档与一次性 Graph 迁移
│   │   ├── memory/                # 记忆系统（第7期）
│   │   │   ├── __init__.py        # 导出 MemoryManager / ShortTermMemory / LongTermMemory
│   │   │   ├── manager.py         # MemoryManager：统一管理短期 + 长期记忆
│   │   │   ├── short_term.py      # 短期记忆：会话内事实提取
│   │   │   ├── long_term.py       # 长期记忆：跨会话持久化（JSON per user）
│   │   │   └── extraction.py      # LLM 事实提取（共用模块）
│   │   ├── skills/                # Skill 模块（第8期）：代码 + 技能内容分层
│   │   │   ├── __init__.py        # 导出 SkillManager / SkillMeta
│   │   │   ├── loader.py          # SkillManager：扫描、发现、加载 SKILL.md（渐进式披露）
│   │   │   └── definitions/       # 技能内容（遵循 Agent Skills 开放标准，每个一个 SKILL.md）
│   │   │       ├── process-return/
│   │   │       │   └── SKILL.md   # 退货退款处理技能（确认订单→校验资格→退款→告知进度）
│   │   │       ├── track-order/
│   │   │       │   └── SKILL.md   # 订单物流跟踪技能（查单→查物流→综合建议）
│   │   │       └── product-recommend/
│   │   │           └── SKILL.md   # 商品推荐技能（了解需求→查偏好→搜索→推荐）
│   │   ├── strategies/            # (upcoming) Agent 执行策略
│   │   ├── tools/                 # 电商工具集（Function Calling）
│   │   │   ├── mock_data.py       # Mock 数据：订单、商品、物流
│   │   │   ├── registry.py        # 本地工具注册表 + OpenAI schema + 分发执行
│   │   │   ├── manager.py         # ToolManager：统一管理本地 + MCP 工具（支持 allowed_tools 过滤）
│   │   │   ├── execution.py       # 同轮同参只读工具结果复用，写操作自动失效
│   │   │   ├── order.py           # 查询订单详情
│   │   │   ├── product.py         # 搜索商品信息
│   │   │   ├── logistics.py       # 查询物流轨迹
│   │   │   ├── refund.py          # 申请退款
│   │   │   ├── knowledge.py       # search_knowledge：RAG 政策/FAQ 检索
│   │   │   ├── memory_tool.py     # recall_user_memory：查询用户记忆
│   │   │   └── skill_tool.py      # load_skill：按需加载技能指令
│   │   └── rag/                   # RAG 模块
│   │       ├── chunker.py         # Markdown → Chunk（按二级标题切分）
│   │       ├── embedder.py        # OpenAI Embeddings 封装
│   │       ├── retriever.py       # KnowledgeRetriever：query → 向量检索
│   │       ├── backends/          # 向量后端（可切换）
│   │       │   ├── base.py        # VectorBackend 抽象接口
│   │       │   ├── numpy_backend.py   # 手写余弦 + JSON（教学透明，零依赖）
│   │       │   └── chroma_backend.py  # Chroma 嵌入式向量数据库（生产代表）
│   │       └── knowledge/         # 知识库源文档（markdown，RAG 数据源）
│   │           ├── 退换货政策.md
│   │           ├── 配送说明.md
│   │           ├── 会员权益.md
│   │           └── 常见问题FAQ.md
│   ├── mcp_client/                # MCP Client（同步封装）
│   │   ├── client.py              # MCPClient：后台线程管理异步连接
│   │   └── converter.py           # MCP Tool schema → OpenAI function calling 格式
│   ├── evaluation/                # Agent 评估体系（第9期）
│   │   ├── __init__.py            # 导出 EvalCase / Sandbox / Evaluator / RunTrace 等
│   │   ├── dataset.py             # EvalCase 数据结构 + load_dataset
│   │   ├── trace.py               # RunTrace：沙箱采集的过程+结果载体
│   │   ├── sandbox.py             # Sandbox：隔离环境 + 共享 client 插桩 + 采集
│   │   ├── metrics.py             # 过程/结果双层指标（代码规则 + LLM judge）
│   │   ├── evaluator.py           # Evaluator：跑用例 → 双层评分 → 聚合报告
│   │   ├── cases.json             # 50 条分层 Agent 黄金测试集（引用 mock 数据）
│   │   ├── rag_dataset.py         # RAG 用例结构与标注校验
│   │   ├── rag_metrics.py         # Recall@K / MRR 确定性指标
│   │   ├── rag_evaluator.py       # 检索 + 回答完整性 + Faithfulness 聚合
│   │   └── rag_cases.json         # 30 条 RAG 专项集，覆盖全部 19 个知识 Chunk
│   ├── multi_agent/               # Multi-Agent 协作（第6期）
│   │   ├── router.py              # 意图路由器（LLM 分类 → 子 Agent）
│   │   ├── agents.py              # SubAgent 子 Agent 类 + 配置
│   │   └── orchestrator.py        # Multi-Agent 的 EcomAgent/Graph 入口
│   ├── scripts/
│   │   ├── build_kb_index.py      # 离线构建知识库索引（--backend numpy/chroma）
│   │   ├── run_eval.py            # 单变体评估（baseline/optimized）
│   │   ├── compare_eval.py        # 前后真实重跑对比 + 可审计报告
│   │   ├── rescore_eval.py        # 历史 trace 离线重评（不重新调用模型）
│   │   └── run_rag_eval.py        # RAG Recall@K / MRR / Faithfulness 评估
│   └── sessions/                  # 运行时生成，已 .gitignore
│       ├── session.json           # 旧版会话迁移归档
│       ├── kb_index.json          # NumpyBackend 索引
│       ├── chroma/                # ChromaBackend 持久化目录
│       └── memory/                # 长期记忆存储（按 user_id 分文件）
│           └── {user_id}.json
│
├── mcp_server/                    # MCP Server（独立微服务）
│   └── server.py                  # FastMCP + Streamable HTTP，暴露电商工具
│
└── tests/                         # 全部测试
    ├── test_conversation_management.py  # 多轮对话管理
    ├── test_react_agent.py        # ReAct Agent + Function Calling
    ├── test_mcp.py                # MCP 集成
    ├── test_rag.py                # RAG 知识库检索
    ├── test_rag_evaluation.py     # RAG 数据覆盖与指标聚合
    ├── test_multi_agent.py        # Multi-Agent 协作
    ├── test_memory.py             # Memory 短期记忆 & 长期记忆
    ├── test_skills.py             # Skill 可复用能力模块
    └── test_evaluation.py         # Agent 评估体系（沙箱 + 双层测评）
```

## 测试与生产部署

默认 `python -m pytest -q` 是完全离线门禁；在线测试使用显式 `--suite`。
LangSmith、工具白名单、Docker、CI 和回滚说明见 [运维指南](docs/operations.md)。
