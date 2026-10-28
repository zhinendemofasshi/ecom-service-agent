# Ecom-Service-Agent: E-commerce Customer Service Agent

**English** | [简体中文](README.zh-CN.md)

## Quick Start

```bash
# 1. Enter the project and create a virtual environment (Python 3.11+)
cd ecom-service-agent
python3.11 -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your API key
cp .env.example .env
# Edit .env and set at least:
#   OPENAI_API_KEY=sk-your-key
#   (optional) OPENAI_BASE_URL=https://...  if you use a relay/proxy
#   (optional) MODEL_NAME=gpt-4o-mini

# 4. Build the knowledge base index (needed for RAG; run once)
python -m app.scripts.build_kb_index

# 5. Start chatting
python main.py
```

Once it starts, just type a question. Try these (the demo data and knowledge base are in Chinese):

- `我的订单还没发货，怎么回事？` ("My order hasn't shipped yet, what's going on?") — triggers order + logistics lookup
- `有没有宽松透气的裤子推荐？` ("Any loose, breathable trousers you'd recommend?") — triggers the product recommendation skill
- `这件衣服质量有问题，我要退货` ("This item has a quality problem, I want to return it") — triggers the return & refund flow

Each reply ends with `[intent | confidence | escalate to human]`. These commands are also available in the chat:

| Command         | Effect                              |
| --------------- | ----------------------------------- |
| `skills`        | List loaded skill modules           |
| `memory`        | Show short-term / long-term memory  |
| `reset`         | Clear the current session           |
| `quit` / `exit` | Exit                                |

**Advanced features** (optional; edit `.env` and restart):

- `AGENT_RUNTIME=langgraph` — LangGraph + Responses API is the default; set `legacy` to fall back to the old ReAct loop
- `HITL_ENABLED=true` — durably pause before `apply_refund` runs; type `approve` / `reject` to resume
- `MULTI_AGENT_ENABLED=true` — multi-agent collaboration (pre-sales / after-sales / complaints routing)
- `MCP_ENABLED=true` — call tools over the MCP protocol (requires running `python mcp_server/server.py` separately)
- `RAG_BACKEND=chroma` — use the Chroma vector database instead (requires `pip install chromadb`)

LangGraph checkpoints are written to `app/sessions/langgraph-checkpoints.sqlite` by default, so pending approvals survive a process restart. SQLite is meant for local development; `CHECKPOINT_BACKEND=postgres` switches checkpoints, API/events, refund approvals, sessions and memory storage to PostgreSQL together. With `MULTI_AGENT_ENABLED`, the Router, pre-sales, after-sales and complaint agents all run inside one LangGraph and share checkpoints. For the P1 implementation, approval usage and limits, see the [P1 acceptance notes](docs/p1-audit.md) (Chinese).

### API, SSE and Production Configuration

Configure `AUTH_TOKENS` (high-entropy login credentials issued/configured server-side) and `REFUND_APPROVER_IDS`, then start the server. PostgreSQL mode supports multiple workers; SQLite mode stays single-process:

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000 --workers 2
```

Requests carry `Authorization: Bearer <token>` by default. `AUTH_TOKENS` is a JSON object whose keys are high-entropy tokens and whose values contain `tenant_id`, `user_id`, `actor_id`, `roles` and `expires_at` (Unix seconds). Identity is read from this server-side mapping and client identity headers are ignored; expired credentials return 401. Approvers need the `approver` role **and** must be listed in `REFUND_APPROVER_IDS`; administrators use the `admin` role. For an agent's approval token, `user_id` is the order owner being served and `actor_id` is the actual approver. Credentials are configured by a trusted login system or an administrator; this project provides no password login or external IdP.

Set `TRUSTED_IDENTITY_HEADERS=true` only behind a trusted gateway. It enables the legacy `X-API-Key` / `X-Tenant-ID` / `X-User-ID` / `X-Actor-ID` headers, with `X-Approval-Key` for approvals and `X-Admin-Key` for admin. The gateway must overwrite any identity headers sent by the client; this mode is off by default.

Main endpoints:

| Endpoint                                                           | Purpose                                                                 |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `POST /sessions`                                                   | Create an isolated session                                              |
| `POST /sessions/{id}/messages`                                     | Send a message and start an agent run                                   |
| `GET /sessions/{id}/events?after=0`                                | SSE stream of token, tool and approval events; `after` resumes after a disconnect |
| `POST /sessions/{id}/approval`                                     | `approve` / `edit` / `reject` a sensitive action                        |
| `POST /runs/{request_id}/approve`, `POST /runs/{request_id}/reject` | Approve per run; duplicate or stale approvals return 409               |
| `POST /runs/{request_id}/cancel`                                   | Cancel a pending approval without calling the model; a running run stops at the next checkpoint |
| `GET /runs/{request_id}`                                           | Query running, awaiting-approval, completed, failed or cancelled status |
| `POST /admin/checkpoints/cleanup`                                  | Delete completed checkpoints past the retention period                  |
| `GET /health`, `GET /ready`                                        | Liveness and dependency readiness checks                                |

PostgreSQL mode uses unified shared storage and database execution locks. For migrating old data, lifecycle cleanup and integration tests, see [PostgreSQL operation and migration](docs/postgres.md):

```dotenv
CHECKPOINT_BACKEND=postgres
DATABASE_URL=postgresql://user:password@postgres:5432/ecom_agent?sslmode=require
```

Responses API text deltas feed both live CLI printing and persisted SSE events. The message POST waits until completion, but you can connect to the events endpoint at the same time to read tokens. Set `X-Request-ID` up front so that after a disconnect you can query that run instead of resubmitting. Dropping the POST connection does not stop the background run. SSE accepts `Last-Event-ID` or the preferred `after` parameter; expired cursors return 410 and invalid ones 422. `app.api_client.session_events` provides auto-reconnect, backoff and cursor de-duplication; callers should persist the `sequence` of handled events. The service keeps the latest 2000 events.

All tools go through timeouts, exponential-backoff retries for transient errors on read-only calls, circuit breaking, and a process-wide pool of 32 execution slots. Argument errors are not retried and writes are never retried automatically. A thread timeout cannot undo a write that was already sent, so a timed-out task keeps its slot until it actually exits, and write results should be reconciled with a business idempotency key. The refund service stores approval state independently and verifies order ownership, the approver allowlist, approval validity, amount/count limits and the idempotency key, recording the refund and audit log in one transaction. Cancelling requires no further model call; approval validity is checked both at decision time and at execution time, independent of the graph's pause logic.

Orders still come from `mock_data.ORDERS`, and a refund means a locally committed request — no payment channel is connected. For Docker and real order/payment system deployment acceptance, see the [P0 audit record](docs/p0-audit.md) (Chinese).

**Running evaluations & tests**:

```bash
python -m app.scripts.run_eval --mode multi --variant optimized --no-judge
python -m app.scripts.run_eval --case-id knowledge_account_stolen --no-judge
python -m app.scripts.compare_eval    # re-run baseline/optimized for real and generate JSON + Markdown
python -m app.scripts.rescore_eval docs/evaluation-results/2026-08-15/optimized.json
python -m app.scripts.run_rag_eval --no-judge  # 30 cases, Recall@1/3/5 + MRR
python -m app.scripts.run_rag_eval --judge --judge-model gpt-5.6-luna  # + Faithfulness
pytest                                # run all unit tests
```

The golden set has 50 cases covering orders, logistics, products, knowledge base, refunds, complaints and general conversation. The comparison report shows pass rate, routing accuracy, tool Precision/Recall/F1, input/output tokens, estimated API cost, and mean/P50/P95 latency. Reports (in Chinese):

- Design and reproduction details: [`docs/第10期-Agent评估工程化增强.md`](docs/第10期-Agent评估工程化增强.md)
- Real A/B results on 50 cases (2026-08-15): [`comparison.md`](docs/evaluation-results/2026-08-15/comparison.md)
- Pass-rate error attribution and quality fixes (62% → 96%): [`quality-fix-comparison.md`](docs/evaluation-results/2026-08-15/quality-fix-comparison.md)
- Model selection, GPT-4o mini vs GPT-5.6 Luna on the same set: [`model-comparison.md`](docs/evaluation-results/2026-08-15/model-comparison.md)
- 30-case RAG evaluation covering all 19 knowledge chunks (Recall@3/5 98.3%/100%, MRR 0.95, Faithfulness 100%): [`rag-evaluation.md`](docs/evaluation-results/2026-08-15/rag-evaluation.md)

### Architecture

```
ecom-service-agent/
├── main.py                        # CLI entry (single-agent / multi-agent mode + memory/skills commands)
├── requirements.txt
├── .env.example
│
├── app/                           # All main bot code + data
│   ├── config/
│   │   └── settings.py            # Config from .env (MCP / RAG / Multi-Agent / Memory / Skill / Evaluation)
│   ├── prompts/
│   │   ├── customer_service.py    # Customer service system prompt (tool usage guide + memory)
│   │   ├── summarizer.py          # History summary prompt
│   │   ├── agents.py              # Multi-agent sub-agent prompts (pre-sales / after-sales / complaints + Router)
│   │   ├── memory.py              # Memory extraction prompt (short-term / long-term facts)
│   │   └── evaluation.py          # LLM-as-judge prompt (answer quality / hallucination / process)
│   ├── schemas/
│   │   └── response.py            # Structured output schema (Pydantic)
│   ├── agent/                     # Agent core + full agent stack (tools / rag / skills)
│   │   ├── chat.py                # Core ReAct loop (with MemoryManager + SkillManager)
│   │   ├── summarizer.py          # LLM self-compression of old turns (handles tool messages)
│   │   ├── storage.py             # Legacy session archive and one-off graph migration
│   │   ├── memory/                # Memory system (part 7)
│   │   │   ├── __init__.py        # Exports MemoryManager / ShortTermMemory / LongTermMemory
│   │   │   ├── manager.py         # MemoryManager: unified short- + long-term memory
│   │   │   ├── short_term.py      # Short-term memory: in-session fact extraction
│   │   │   ├── long_term.py       # Long-term memory: cross-session persistence (JSON per user)
│   │   │   └── extraction.py      # LLM fact extraction (shared)
│   │   ├── skills/                # Skill module (part 8): code and skill content separated
│   │   │   ├── __init__.py        # Exports SkillManager / SkillMeta
│   │   │   ├── loader.py          # SkillManager: discover and load SKILL.md (progressive disclosure)
│   │   │   └── definitions/       # Skill content (Agent Skills open standard, one SKILL.md each)
│   │   │       ├── process-return/
│   │   │       │   └── SKILL.md   # Returns & refunds (confirm order → check eligibility → refund → report)
│   │   │       ├── track-order/
│   │   │       │   └── SKILL.md   # Order tracking (look up order → logistics → advice)
│   │   │       └── product-recommend/
│   │   │           └── SKILL.md   # Product recommendation (needs → preferences → search → recommend)
│   │   ├── strategies/            # (upcoming) Agent execution strategies
│   │   ├── tools/                 # E-commerce toolset (function calling)
│   │   │   ├── mock_data.py       # Mock data: orders, products, logistics
│   │   │   ├── registry.py        # Local tool registry + OpenAI schema + dispatch
│   │   │   ├── manager.py         # ToolManager: local + MCP tools (allowed_tools filtering)
│   │   │   ├── execution.py       # Reuse read-only results within a turn; writes invalidate
│   │   │   ├── order.py           # Order details
│   │   │   ├── product.py         # Product search
│   │   │   ├── logistics.py       # Logistics tracking
│   │   │   ├── refund.py          # Refund requests
│   │   │   ├── knowledge.py       # search_knowledge: RAG policy/FAQ retrieval
│   │   │   ├── memory_tool.py     # recall_user_memory: query user memory
│   │   │   └── skill_tool.py      # load_skill: load skill instructions on demand
│   │   └── rag/                   # RAG module
│   │       ├── chunker.py         # Markdown → chunks (split on H2)
│   │       ├── embedder.py        # OpenAI Embeddings wrapper
│   │       ├── retriever.py       # KnowledgeRetriever: query → vector search
│   │       ├── backends/          # Pluggable vector backends
│   │       │   ├── base.py        # VectorBackend interface
│   │       │   ├── numpy_backend.py   # Hand-written cosine + JSON (transparent, zero deps)
│   │       │   └── chroma_backend.py  # Chroma embedded vector DB (production example)
│   │       └── knowledge/         # Knowledge base source docs (Chinese markdown)
│   │           ├── 退换货政策.md      # Return policy
│   │           ├── 配送说明.md        # Shipping info
│   │           ├── 会员权益.md        # Membership benefits
│   │           └── 常见问题FAQ.md     # FAQ
│   ├── mcp_client/                # MCP client (sync wrapper)
│   │   ├── client.py              # MCPClient: async connection on a background thread
│   │   └── converter.py           # MCP tool schema → OpenAI function calling format
│   ├── evaluation/                # Agent evaluation (part 9)
│   │   ├── __init__.py            # Exports EvalCase / Sandbox / Evaluator / RunTrace etc.
│   │   ├── dataset.py             # EvalCase + load_dataset
│   │   ├── trace.py               # RunTrace: process + result captured by the sandbox
│   │   ├── sandbox.py             # Sandbox: isolation + shared client instrumentation + capture
│   │   ├── metrics.py             # Process/result metrics (code rules + LLM judge)
│   │   ├── evaluator.py           # Evaluator: run cases → two-layer scoring → report
│   │   ├── cases.json             # 50-case layered golden set (uses mock data)
│   │   ├── rag_dataset.py         # RAG case structure and label validation
│   │   ├── rag_metrics.py         # Recall@K / MRR deterministic metrics
│   │   ├── rag_evaluator.py       # Retrieval + answer completeness + Faithfulness
│   │   └── rag_cases.json         # 30 RAG cases covering all 19 knowledge chunks
│   ├── multi_agent/               # Multi-agent collaboration (part 6)
│   │   ├── router.py              # Intent router (LLM classification → sub-agent)
│   │   ├── agents.py              # SubAgent class + config
│   │   └── orchestrator.py        # Multi-agent EcomAgent/graph entry
│   ├── scripts/
│   │   ├── build_kb_index.py      # Build knowledge base index offline (--backend numpy/chroma)
│   │   ├── run_eval.py            # Single-variant evaluation (baseline/optimized)
│   │   ├── compare_eval.py        # Real before/after re-run + auditable report
│   │   ├── rescore_eval.py        # Offline re-scoring of saved traces (no model calls)
│   │   └── run_rag_eval.py        # RAG Recall@K / MRR / Faithfulness evaluation
│   └── sessions/                  # Generated at runtime, git-ignored
│       ├── session.json           # Legacy session migration archive
│       ├── kb_index.json          # NumpyBackend index
│       ├── chroma/                # ChromaBackend persistence
│       └── memory/                # Long-term memory (one file per user_id)
│           └── {user_id}.json
│
├── mcp_server/                    # MCP server (standalone service)
│   └── server.py                  # FastMCP + Streamable HTTP exposing e-commerce tools
│
└── tests/                         # All tests
    ├── test_conversation_management.py  # Multi-turn conversation management
    ├── test_react_agent.py        # ReAct agent + function calling
    ├── test_mcp.py                # MCP integration
    ├── test_rag.py                # RAG retrieval
    ├── test_rag_evaluation.py     # RAG data coverage and metric aggregation
    ├── test_multi_agent.py        # Multi-agent collaboration
    ├── test_memory.py             # Short-term & long-term memory
    ├── test_skills.py             # Reusable skill modules
    └── test_evaluation.py         # Agent evaluation (sandbox + two-layer scoring)
```

## Testing and Production Deployment

By default `python -m pytest -q` is a fully offline gate; online tests use an explicit `--suite`.
For LangSmith, tool allowlists, Docker, CI and rollback, see the [operations guide](docs/operations.md) (Chinese).
