from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str

    # Embedding service
    embedding_api_url: str
    embedding_model: str = "bge-large-en-v1.5"
    batch_size: int = 32
    embedding_dimension: int = 1024

    # LLM service
    llm_provider: str = "ollama"
    llm_base_url: str = "http://100.120.187.84:11434"
    llm_model: str = "deepseek-coder:6.7b"
    llm_timeout: int = 60
    llm_max_retries: int = 2
    reasoning_model: str = "qwen3:4b"
    reasoning_temperature: float = 0.6
    reasoning_timeout: int = 45
    react_max_iterations: int = 4
    sql_generation_timeout: int = 90
    sql_dialect: str = "mysql"

    # HTTP client behaviour
    embed_timeout: float = 30.0
    embed_max_retries: int = 3
    embed_retry_base_delay: float = 1.0

    # Retrieval
    top_k: int = 5
    embed_cache_ttl_seconds: int = 3600
    sql_cache_ttl_seconds: int = 3600
    sql_cache_enabled: bool = True
    embed_cache_enabled: bool = True
    ask_cache_ttl_seconds: int = 300
    ask_cache_enabled: bool = True
    ask_cache_semantic_threshold: float = 0.97
    sql_cache_semantic_threshold: float = 0.96
    min_pattern_use_count: int = 2
    min_instruction_confidence: float = 0.5
    query_rewrite_enabled: bool = True
    query_rewrite_model: str = "deepseek-coder:6.7b"
    query_rewrite_timeout: int = 8
    query_rewrite_max_tokens: int = 120
    query_rewrite_hints: str = "counselor,counsellor,counsellors -> employee"

    # App DB for live schema introspection (optional for enrichment)
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str | None = None
    db_central: str | None = None
    db_reconnect_min_interval: float = 5.0

    # Answer generation settings for /ask
    answer_model: str | None = None
    answer_timeout: int = 45
    answer_temperature: float = 0.2
    answer_max_tokens: int = 300
    answer_max_words: int = 80
    answer_allow_reasoning: bool = False
    answer_strict_concise: bool = True
    ask_timeout: int = 105

    # Governance / rulebook system
    governance_enabled: bool = True
    governance_enabled_rules: str = "all"
    governance_inject_react: bool = True
    governance_inject_sql: bool = True
    governance_inject_answer: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
