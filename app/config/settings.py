from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """项目配置，从 .env 文件读取"""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.7

    # Agent 执行层：LangGraph + Responses API 为默认；legacy 保留回滚路径。
    agent_runtime: str = "langgraph"
    langgraph_checkpoint_path: str = "app/sessions/langgraph-checkpoints.sqlite"
    checkpoint_backend: str = "sqlite"
    database_url: str = ""
    checkpoint_retention_days: int = 30
    checkpoint_cleanup_interval_seconds: int = 3600
    abandoned_run_seconds: int = 300
    hitl_enabled: bool = True
    approval_timeout_seconds: int = 86_400

    # Tool execution reliability.
    tool_timeout_seconds: float = 10.0
    tool_max_retries: int = 2
    tool_circuit_failure_threshold: int = 3
    tool_circuit_recovery_seconds: float = 30.0
    tool_max_input_bytes: int = 64_000
    tool_max_output_bytes: int = 1_000_000

    # API/auth and durable refund audit storage.
    # Opaque login tokens provisioned by the trusted identity service.
    auth_tokens: dict[str, dict] = {}
    trusted_identity_headers: bool = False
    refund_approver_ids: list[str] = []
    service_api_key: str = "change-me"
    approver_api_key: str = "change-me-approver"
    admin_api_key: str = "change-me-admin"
    refund_db_path: str = "app/sessions/refunds.sqlite"
    max_refund_amount: float = 10_000.0
    max_refunds_per_user_per_day: int = 5
    api_session_db_path: str = "app/sessions/api-sessions.sqlite"
    max_concurrent_runs: int = 32

    # ReAct 循环
    max_react_steps: int = 5

    # MCP 配置
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"

    # RAG 配置（第5期）
    embedding_model: str = "text-embedding-3-small"
    kb_dir: str = "app/agent/rag/knowledge"
    # 向量后端：numpy（手写余弦，教学透明，零依赖，默认）/ chroma（向量数据库，生产代表，需 pip install chromadb）
    rag_backend: str = "numpy"
    # NumpyBackend 的 JSON 索引路径
    kb_index_path: str = "app/sessions/kb_index.json"
    # ChromaBackend 的持久化目录与 collection 名
    chroma_persist_dir: str = "app/sessions/chroma"
    chroma_collection: str = "ecom_kb"

    # Multi-Agent 配置（第6期）
    multi_agent_enabled: bool = False
    cli_actor_id: str = ""
    router_strategy: str = "hybrid"
    guardrails_enabled: bool = True
    context_token_budget: int = 24000
    conversation_retention_days: int = 90
    memory_retention_days: int = 365
    approval_poll_seconds: int = 15

    # Memory 配置（第7期）
    memory_enabled: bool = True
    memory_dir: str = "app/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50

    # Skill 配置（第8期）
    skills_enabled: bool = True
    skills_dir: str = "app/agent/skills/definitions"

    # Evaluation 配置（第9期，离线评估工具，无聊天开关）
    eval_dataset_path: str = "app/evaluation/cases.json"
    eval_use_judge: bool = True  # 是否启用 LLM-as-judge（质量/幻觉/过程合理性）
    eval_pass_threshold: float = 0.6  # 单维度通过阈值（judge 归一化到 0-1 后比较）
    eval_temperature: float = 0.0  # A/B 固定采样温度，避免把随机性误算为优化效果
    # 成本估算单价（USD / 1M tokens）。跑评估时会把单价写入报告，便于复现。
    # 默认对应 gpt-4o-mini 公开标准价；换模型时应在 .env 中同步修改。
    eval_input_cost_per_million: float = 0.15
    eval_output_cost_per_million: float = 0.60

    # 多轮对话管理
    session_path: str = "app/sessions/session.json"
    history_threshold: int = 10  # 消息压缩策略通常为上下文达到一定的token数，例如claude code通常为达到最大上下文窗口的70%左右，此处简略为原始消息条数超过10轮
    history_keep_recent: int = 3  # 压缩时保留最近 3 条原始消息

    @property
    def state_database_url(self) -> str:
        if self.checkpoint_backend == "postgres":
            if not self.database_url:
                raise ValueError("PostgreSQL 模式必须配置 DATABASE_URL")
            return self.database_url
        return ""

    @property
    def refund_store(self) -> str:
        return self.state_database_url or self.refund_db_path

    model_config = {"env_file": ".env"}


settings = Settings()
