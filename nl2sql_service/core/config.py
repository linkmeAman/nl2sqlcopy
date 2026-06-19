from __future__ import annotations

import json
import os
from pathlib import Path
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_OPENAI_COMPATIBLE = {"openai", "groq", "openrouter", "together", "togetherai"}
_API_KEY_REQUIRED = _OPENAI_COMPATIBLE | {
    "anthropic",
    "claude",
    "gemini",
    "google",
    "voyage",
    "voyageai",
}
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "google": "gemini",
    "together": "togetherai",
    "voyageai": "voyage",
}
_EMBEDDING_CUSTOM_PROVIDERS = {"custom", "http", "tei", "external"}


def _normalize_provider(provider: str | None) -> str:
    cleaned = (provider or "").strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(cleaned, cleaned)


def _resolve_secret_reference(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("file:"):
        path = cleaned.removeprefix("file:")
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip() or None
        except OSError:
            return None
    if cleaned.startswith("env:"):
        return os.getenv(cleaned.removeprefix("env:"), "").strip() or None
    return cleaned


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str

    # Embedding service. The default "custom" provider preserves the existing
    # external bge-large FastAPI/TEI endpoint contract. For lower latency,
    # bge-small-en-v1.5 can be used with EMBEDDING_DIMENSION=384.
    embedding_api_url: str | None = None
    embedding_provider: str = "custom"
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str = "bge-large-en-v1.5"
    batch_size: int = 32
    embedding_dimension: int = 1024

    # LLM service
    llm_provider: str = "ollama"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "deepseek-coder:6.7b"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout: int = 60
    llm_max_retries: int = 2
    llm_retry_base_delay: float = 0.5
    llm_fallback_provider: str | None = None
    llm_fallback_model: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_base_url: str | None = None
    provider_key_encryption_secret: str | None = None
    ollama_default_base_url: str = "http://localhost:11434"
    anthropic_default_base_url: str = "https://api.anthropic.com/v1"
    gemini_default_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    voyage_default_base_url: str = "https://api.voyageai.com/v1"
    openai_default_base_url: str = "https://api.openai.com/v1"
    health_probe_model_anthropic: str = "claude-3-5-haiku-latest"
    health_probe_timeout_seconds: float = 10.0
    health_probe_timeout_clamp: float = 10.0
    health_probe_max_tokens: int = 64

    # Role-specific model routing. Any unset value falls back to LLM_*.
    sql_model_provider: str | None = None
    sql_model: str | None = None
    sql_model_api_key: str | None = None
    sql_model_base_url: str | None = None
    sql_fallback_provider: str | None = None
    sql_fallback_model: str | None = None
    sql_fallback_api_key: str | None = None
    sql_fallback_base_url: str | None = None

    reasoning_model_provider: str | None = None
    reasoning_model: str = "qwen3:4b"
    reasoning_model_api_key: str | None = None
    reasoning_model_base_url: str | None = None
    reasoning_fallback_provider: str | None = None
    reasoning_fallback_model: str | None = None
    reasoning_fallback_api_key: str | None = None
    reasoning_fallback_base_url: str | None = None
    reasoning_temperature: float = 0.6
    reasoning_timeout: int = 45
    react_max_iterations: int = 2
    react_confidence_threshold: float = 0.75
    react_confidence_tables_weight: float = 0.35
    react_confidence_join_paths_weight: float = 0.2
    react_confidence_group_weight: float = 0.15
    react_confidence_example_weight: float = 0.2
    react_confidence_iteration_penalty: float = 0.08
    react_past_corrections_limit: int = 3
    react_past_corrections_similarity: float = 0.75
    react_join_path_limit: int = 5
    react_relation_retrieval_top_k: int = 8
    react_sample_query_limit: int = 3
    react_reasoning_max_tokens: int = 800
    react_planner_max_tokens: int = 300
    react_top_k_multiplier: int = 4
    react_top_k_floor: int = 8
    sql_generation_timeout: int = 90
    sql_generation_max_tables: int = Field(
        default=5,
        description="Max tables passed to SQL generation context.",
    )
    ambiguity_query_stopwords: str = (
        "a,all,an,and,by,fetch,find,for,from,get,give,list,me,of,please,search,"
        "show,summarize,the,their,these,those,with"
    )
    ambiguity_generic_terms: str = (
        "data,detail,details,entries,entry,info,information,item,items,record,"
        "records,report,reports,results,rows"
    )
    ambiguity_modifier_terms: str = (
        "active,closed,current,inactive,latest,live,new,newest,old,open,recent"
    )
    destructive_query_keywords: str = (
        "delete,drop,truncate,update,insert,alter,create,grant,revoke"
    )
    react_past_corrections_min_tokens: int = 8
    react_past_corrections_connector_terms: str = "and,join,compare,between,versus,vs,with"
    deterministic_filter_rules_json: str = json.dumps(
        [
            {
                "name": "status_active",
                "query_terms": ["active"],
                "column_terms": ["status", "state", "active"],
                "operator": "=",
                "value": "active",
            },
            {
                "name": "status_inactive",
                "query_terms": ["inactive"],
                "column_terms": ["status", "state", "active"],
                "operator": "=",
                "value": "inactive",
            },
            {
                "name": "email_lookup",
                "query_terms": ["email"],
                "column_terms": ["email", "email_id"],
                "literal_type": "email",
                "fallback": "not_null",
            },
            {
                "name": "number_lookup",
                "query_terms": ["mobile", "phone", "number", "code"],
                "column_terms": [
                    "mobile",
                    "phone",
                    "phone_number",
                    "contact_no",
                    "contact_number",
                    "code",
                    "number",
                ],
                "literal_type": "integer",
                "fallback": "not_null",
            },
        ]
    )
    sql_subcall_max_tokens: int = 150
    sql_dialect: str = "mysql"

    # HTTP client behaviour
    embed_timeout: float = 30.0
    embed_max_retries: int = 3
    embed_retry_base_delay: float = 1.0

    # Retrieval / vector store
    vector_provider: str = "pgvector"
    vector_base_url: str | None = None
    vector_api_key: str | None = None
    vector_hnsw_ef_search: int = 40
    top_k: int = 5
    embed_cache_ttl_seconds: int = 3600
    sql_cache_ttl_seconds: int = 3600
    sql_cache_enabled: bool = True
    embed_cache_enabled: bool = True
    ask_cache_ttl_seconds: int = 300
    ask_cache_enabled: bool = True
    ask_cache_semantic_threshold: float = Field(
        default=0.92,
        validation_alias=AliasChoices(
            "CACHE_SEMANTIC_THRESHOLD_ASK",
            "ASK_CACHE_SEMANTIC_THRESHOLD",
            "ask_cache_semantic_threshold",
        ),
    )
    sql_cache_semantic_threshold: float = 0.96
    min_pattern_use_count: int = 2
    min_instruction_confidence: float = 0.5
    query_rewrite_enabled: bool = True
    query_rewrite_model_provider: str | None = None
    query_rewrite_model: str | None = None
    query_rewrite_fast_model: str | None = None
    query_rewrite_model_api_key: str | None = None
    query_rewrite_model_base_url: str | None = None
    query_rewrite_fallback_provider: str | None = None
    query_rewrite_fallback_model: str | None = None
    query_rewrite_fallback_api_key: str | None = None
    query_rewrite_fallback_base_url: str | None = None
    query_rewrite_timeout: int = 8
    query_rewrite_max_tokens: int = 120
    query_rewrite_hints: str = "counselor,counsellor,counsellors -> employee"
    query_rewrite_synonym_map: str = str(
        Path(__file__).resolve().parent / "data" / "synonyms.json"
    )

    # App DB for live schema introspection (optional for enrichment)
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str | None = None
    db_central: str | None = None
    db_reconnect_min_interval: float = 5.0
    db_pool_max_size: int = 10
    db_pool_command_timeout: int = 30
    db_connect_timeout: int = 10
    db_trace_events_limit_default: int = 500
    db_recent_request_events_limit_default: int = 50

    # Answer generation settings for /ask
    answer_model_provider: str | None = None
    answer_model: str | None = None
    answer_model_api_key: str | None = None
    answer_model_base_url: str | None = None
    answer_fallback_provider: str | None = None
    answer_fallback_model: str | None = None
    answer_fallback_api_key: str | None = None
    answer_fallback_base_url: str | None = None
    answer_timeout: int = 45
    answer_temperature: float = 0.2
    answer_max_tokens: int = 300
    answer_max_words: int = 80
    answer_allow_reasoning: bool = False
    answer_strict_concise: bool = True
    ask_timeout: int = 105
    ask_timeout_clamp_seconds: float = 1.0

    # Backend observability
    observability_service_name: str = "nl2sql-api"
    observability_enabled: bool = True
    observability_queue_size: int = 5000
    observability_batch_size: int = 50
    observability_flush_interval_seconds: float = 0.2
    observability_prompt_char_limit: int = 4000
    observability_sql_char_limit: int = 1000
    observability_sampling_ratio: float = 1.0
    observability_file_logging_enabled: bool = True
    observability_log_dir: str = "logs"
    observability_log_file_basename: str = "nl2sql.log"
    observability_log_retention_days: int = 30
    observability_file_log_level: str = "INFO"
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str | None = None

    # Teach confirmation operational alerts
    teach_pending_active_warn_threshold: int = 25
    teach_pending_expired_warn_threshold: int = 1

    # Startup dependency enforcement
    startup_enforcement_mode: str = "warn"

    # Governance / rulebook system
    governance_enabled: bool = True
    governance_enabled_rules: str = "all"
    governance_inject_react: bool = True
    governance_inject_sql: bool = True
    governance_inject_answer: bool = True
    instruction_min_similarity: float = 0.75
    instruction_min_confidence: float = 0.5
    instruction_retrieval_limit: int = 5
    row_cap_default: int = 50
    telemetry_recent_limit_default: int = 50
    telemetry_trace_limit_default: int = 500

    def provider_readiness_report(self) -> dict[str, object]:
        issues: list[dict[str, str]] = []
        targets: list[dict[str, object]] = []

        def add_issue(code: str, target: str, message: str) -> None:
            issues.append({"code": code, "target": target, "message": message})

        def validate_target(
            *,
            target: str,
            provider: str | None,
            model: str | None,
            api_key: str | None,
            base_url: str | None,
            role: str,
            capability: str,
        ) -> None:
            normalized = _normalize_provider(provider)
            resolved_api_key = _resolve_secret_reference(api_key)
            base_url_configured = bool((base_url or "").strip())
            targets.append(
                {
                    "target": target,
                    "role": role,
                    "capability": capability,
                    "provider": normalized or None,
                    "model": (model or "").strip() or None,
                    "base_url_configured": base_url_configured,
                    "api_key_configured": bool(resolved_api_key),
                }
            )
            if not normalized:
                add_issue("PROVIDER_REQUIRED", target, f"{target} requires a provider.")
            if not (model or "").strip():
                add_issue("MODEL_REQUIRED", target, f"{target} requires a model.")
            if normalized in _API_KEY_REQUIRED and not resolved_api_key:
                add_issue("API_KEY_REQUIRED", target, f"{target} requires a resolved API key.")
            if normalized == "ollama" and not base_url_configured:
                add_issue(
                    "BASE_URL_REQUIRED",
                    target,
                    f"{target} uses Ollama and requires an explicit base URL.",
                )

        validate_target(
            target="LLM_PROVIDER",
            provider=self.llm_provider,
            model=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            role="default",
            capability="generation",
        )
        if _normalize_provider(self.embedding_provider) in _EMBEDDING_CUSTOM_PROVIDERS:
            if not (self.embedding_api_url or "").strip():
                add_issue(
                    "EMBEDDING_API_URL_REQUIRED",
                    "EMBEDDING_PROVIDER",
                    "EMBEDDING_API_URL is required when EMBEDDING_PROVIDER=custom.",
                )
            targets.append(
                {
                    "target": "EMBEDDING_PROVIDER",
                    "role": "embedding",
                    "capability": "embedding",
                    "provider": _normalize_provider(self.embedding_provider) or None,
                    "model": (self.embedding_model or "").strip() or None,
                    "base_url_configured": bool((self.embedding_api_url or "").strip()),
                    "api_key_configured": bool(_resolve_secret_reference(self.embedding_api_key)),
                }
            )
        else:
            embedding_base_url = self.embedding_base_url
            if _normalize_provider(self.embedding_provider) == "ollama" and not embedding_base_url:
                embedding_base_url = self.llm_base_url
            validate_target(
                target="EMBEDDING_PROVIDER",
                provider=self.embedding_provider,
                model=self.embedding_model,
                api_key=self.embedding_api_key,
                base_url=embedding_base_url,
                role="embedding",
                capability="embedding",
            )

        for role in ("sql", "reasoning", "query_rewrite", "answer"):
            provider = getattr(self, f"{role}_model_provider", None) or self.llm_provider
            if role == "answer" and not self.answer_model_provider and not self.answer_model:
                provider = self.reasoning_model_provider or provider

            role_model = getattr(self, f"{role}_model", None) or self.llm_model
            if role == "query_rewrite":
                role_model = self.effective_query_rewrite_model
            if role == "answer" and not self.answer_model_provider and not self.answer_model:
                role_model = self.reasoning_model or role_model

            api_key = getattr(self, f"{role}_model_api_key", None) or self.llm_api_key
            if role == "answer" and not self.answer_model_api_key and not self.answer_model:
                api_key = self.reasoning_model_api_key or api_key

            base_url = getattr(self, f"{role}_model_base_url", None)
            if _normalize_provider(provider) == _normalize_provider(self.llm_provider):
                base_url = base_url or self.llm_base_url
            if role == "answer" and not self.answer_model_base_url and not self.answer_model:
                base_url = self.reasoning_model_base_url or base_url

            validate_target(
                target=f"{role.upper()}_MODEL_PROVIDER",
                provider=provider,
                model=role_model,
                api_key=api_key,
                base_url=base_url,
                role=role,
                capability="generation",
            )

            fallback_provider = getattr(self, f"{role}_fallback_provider", None) or self.llm_fallback_provider
            if not fallback_provider:
                continue
            fallback_model = (
                getattr(self, f"{role}_fallback_model", None)
                or self.llm_fallback_model
                or role_model
            )
            fallback_api_key = (
                getattr(self, f"{role}_fallback_api_key", None)
                or self.llm_fallback_api_key
                or self.llm_api_key
            )
            fallback_base_url = (
                getattr(self, f"{role}_fallback_base_url", None)
                or self.llm_fallback_base_url
            )
            if _normalize_provider(fallback_provider) == _normalize_provider(self.llm_provider):
                fallback_base_url = fallback_base_url or self.llm_base_url
            validate_target(
                target=f"{role.upper()}_FALLBACK_PROVIDER",
                provider=fallback_provider,
                model=fallback_model,
                api_key=fallback_api_key,
                base_url=fallback_base_url,
                role=f"{role}_fallback",
                capability="generation",
            )

        return {
            "status": "ok" if not issues else "error",
            "issues": issues,
            "targets": targets,
        }

    @property
    def effective_query_rewrite_model(self) -> str:
        return (
            (self.query_rewrite_model or "").strip()
            or (self.query_rewrite_fast_model or "").strip()
            or self.llm_model
        )

    def observability_log_dir_path(self) -> Path:
        configured = (self.observability_log_dir or "").strip() or "logs"
        path = Path(configured)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    @model_validator(mode="after")
    def validate_provider_settings(self) -> "Settings":
        report = self.provider_readiness_report()
        issues = report["issues"]
        if issues:
            messages = "; ".join(issue["message"] for issue in issues if isinstance(issue, dict))
            raise ValueError(messages)
        mode = self.startup_enforcement_mode.strip().lower()
        if mode not in {"warn", "strict"}:
            raise ValueError("STARTUP_ENFORCEMENT_MODE must be one of: warn, strict")
        self.startup_enforcement_mode = mode
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
