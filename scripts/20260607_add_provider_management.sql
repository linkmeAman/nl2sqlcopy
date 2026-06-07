CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS nl2sql_llm_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_name VARCHAR(64) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    base_url TEXT,
    org_id TEXT,
    extra_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_local BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nl2sql_llm_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id UUID NOT NULL REFERENCES nl2sql_llm_providers(id),
    key_label VARCHAR(128) NOT NULL,
    api_key_hash TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nl2sql_model_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id UUID NOT NULL REFERENCES nl2sql_llm_providers(id),
    api_key_id UUID REFERENCES nl2sql_llm_api_keys(id),
    model_name VARCHAR(256) NOT NULL,
    display_name VARCHAR(256),
    role VARCHAR(64) NOT NULL DEFAULT 'general',
    context_window INT,
    supports_tools BOOLEAN NOT NULL DEFAULT false,
    supports_stream BOOLEAN NOT NULL DEFAULT true,
    is_default BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    extra_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS nl2sql_model_registry_default_role_idx
ON nl2sql_model_registry(role)
WHERE is_default = true AND is_active = true;
