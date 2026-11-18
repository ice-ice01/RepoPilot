from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TOKEN_ENCRYPTION_KEY = "change-me-in-local-dev"


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "研伴 RepoPilot"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"

    database_url: str = "postgresql+psycopg://devflow:devflow@localhost:5432/devflow"

    github_token: str | None = None
    github_api_base_url: str = "https://api.github.com"

    llm_api_key: str | None = Field(default=None, validation_alias=AliasChoices("AI_API_KEY", "LLM_API_KEY"))
    llm_base_url: str = Field(default="https://api.deepseek.com", validation_alias=AliasChoices("AI_BASE_URL", "LLM_BASE_URL"))
    llm_model: str = Field(default="deepseek-v4-pro", validation_alias=AliasChoices("AI_MODEL", "LLM_MODEL"))
    rag_llm_enabled: bool = True
    embedding_provider: str = "qwen_local"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dimensions: int = 1024
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    local_embedding_device: str | None = None
    local_embedding_revision: str | None = None
    qwen_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    dashscope_api_key: str | None = None
    dashscope_embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    rerank_api_key: str | None = None
    rerank_base_url: str | None = None
    qwen_rerank_model: str = "qwen3-rerank"
    qwen_retrieval_instruction: str = "Given a user question, retrieve passages that contain the facts needed to answer it."

    milvus_enabled: bool = True
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str | None = None
    milvus_collection: str = "devflow_rag_qwen1024"
    rag_rerank_top_n: int = 24
    ragas_enabled: bool = True
    ragas_judge_api_key: str | None = None
    ragas_judge_base_url: str | None = None
    ragas_judge_model: str | None = None
    ragas_timeout_seconds: float = 120.0
    # 单次 Judge HTTP 请求与完整指标（可能含多次请求）使用不同预算。
    ragas_metric_timeout_seconds: float = Field(default=360.0, ge=1, le=1800)
    ragas_metric_concurrency: int = Field(default=2, ge=1, le=7)
    ragas_judge_max_tokens: int = 8192
    ragas_answer_relevancy_strictness: int = 1
    ragas_max_cases: int = 5
    ragas_context_precision_min: float = 0.75
    ragas_context_recall_min: float = 0.80
    ragas_faithfulness_min: float = 0.90
    ragas_answer_relevancy_min: float = 0.80
    ragas_agent_goal_accuracy_min: float = 0.80
    ragas_tool_call_accuracy_min: float = 0.90
    ragas_tool_call_f1_min: float = 0.90
    # 上下文预算：以下是默认值，可通过同名大写环境变量覆盖。
    # dynamic=true 时按模型窗口动态扩展，24000 并不是所有请求的固定硬上限。
    context_max_input_tokens: int = 24000
    context_reserved_response_tokens: int = 3000
    context_dynamic_model_window_enabled: bool = True
    context_model_window_tokens: int | None = None
    context_compact_reserved_output_tokens: int = 12000
    context_auto_compact_enabled: bool = True
    context_warning_buffer_tokens: int = 20000
    context_auto_compact_buffer_tokens: int = 13000
    context_aggressive_buffer_tokens: int = 8000
    context_manual_buffer_tokens: int = 3000
    context_max_compaction_failures: int = 3
    context_microcompact_keep_recent_messages: int = 8
    context_compact_keep_recent_messages: int = 8
    context_compact_summary_tokens: int = 1800
    # 五类输入的分配比例（总和 1.0）；先扣掉响应预留，再按比例分配。
    # 这些是分块裁剪预算，最终提示词还包含技能、规则、工具 schema 等额外文本。
    context_system_ratio: float = 0.12
    context_memory_ratio: float = 0.22
    context_evidence_ratio: float = 0.34
    context_recent_ratio: float = 0.22
    context_tool_ratio: float = 0.10
    workflow_task_timeout_seconds: float = 90.0
    planner_timeout_seconds: float = Field(default=30.0, ge=1, le=180)
    planner_max_tokens: int = Field(default=2048, ge=256, le=8192)
    prompt_cache_enabled: bool = True
    prompt_cache_key: str | None = "devflow-agent-v1"
    prompt_cache_retention: str | None = None
    repo_checkout_dir: str = ".data/repos"
    upload_dir: str = ".data/uploads"

    token_encryption_key: str = DEFAULT_TOKEN_ENCRYPTION_KEY
    demo_mode: bool = True
    # 会话时长以小时为单位，上限 2160（90 天）。
    auth_session_hours: int = Field(default=720, ge=1, le=2160)
    auth_cookie_secure: bool = False
    auth_allow_registration: bool = False
    auth_bootstrap_token: str | None = None
    github_webhook_secret: str | None = None
    feishu_feedback_webhook_url: str | None = None
    feedback_trace_base_url: str | None = None
    auto_sync_enabled: bool = True
    auto_sync_interval_seconds: int = 300
    auto_sync_limit: int = 50
    # 调度器同一时刻并行同步的仓库数上限。
    auto_sync_concurrency: int = Field(default=3, ge=1, le=10)
    # Webhook 触发同步的最小间隔（防重放/风暴）；同一仓库间隔内的重复推送会被跳过。
    webhook_sync_cooldown_seconds: int = Field(default=30, ge=0)
    # 仓库软删除保留期（小时）：期间可恢复，到期后由调度器彻底清理。
    repo_soft_delete_retention_hours: int = Field(default=168, ge=1)
    # 反向代理场景下信任的代理地址：留空表示不信任任何 X-Forwarded-For；
    # "loopback" 表示仅信任本机回环代理；也可以是逗号分隔的 IP/CIDR 列表。
    trusted_proxies: str = ""
    # 每用户每分钟可发起的 LLM 消耗型请求上限（聊天与 RAGAS 评测），0 表示关闭限流。
    llm_request_rate_limit_per_minute: int = Field(default=60, ge=0)
    # PBKDF2 密码哈希迭代数：测试环境可降到 2000 提速；生产保持默认 600000。
    pbkdf2_iterations: int = Field(default=600_000, ge=1_000, le=2_000_000)
    # 日志：text 普通格式 / json 结构化；日志记录会自动带上请求 ID。
    log_level: str = "INFO"
    log_format: str = "text"
    # RAG 检索进程内 TTL 缓存；TTL 为 0 表示关闭。写入/删除/重建索引时自动失效。
    rag_retrieval_cache_ttl_seconds: int = Field(default=60, ge=0)
    rag_retrieval_cache_max_entries: int = Field(default=256, ge=1)

    @model_validator(mode="after")
    def refuse_default_secrets_in_production(self) -> "Settings":
        # 生产环境继续使用公开默认密钥会让 GitHub Token 加密形同明文，必须直接拒绝启动。
        if self.app_env.strip().lower() == "production" and self.token_encryption_key == DEFAULT_TOKEN_ENCRYPTION_KEY:
            raise ValueError(
                "APP_ENV=production 时必须设置随机的 TOKEN_ENCRYPTION_KEY，"
                "不能使用内置默认值（详见 .env.example）"
            )
        return self

    @field_validator("llm_base_url")
    @classmethod
    def normalize_llm_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        # A website origin needs the OpenAI-compatible API prefix; preserve custom paths.
        path = parts.path.rstrip("/") or "/v1"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )


settings = Settings()
