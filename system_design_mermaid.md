# NL2SQL System Design (Important Points)

This document provides a scalable, interface-level architecture for the current NL2SQL project.

## 1) Complete System Diagram

```mermaid
flowchart LR
  %% Clients
  subgraph C[Clients]
    C1[Backend Apps]
    C2[CLI Scripts]
    C3[Browser Users]
    C4[Ops and CI]
  end

  %% API Layer
  subgraph API[FastAPI Service]
    A0[Router and Middleware]
    A1[Help APIs<br/>GET /help, /help/module, /help/module/route]
    A2[Ingestion APIs<br/>POST /ingest<br/>POST /ingest/groups<br/>POST /ingest/knowledge<br/>POST /ingest/patterns<br/>POST /ingest/instructions]
    A3[Retrieval APIs<br/>POST /query<br/>POST /query/groups]
    A4[Generation API<br/>POST /generate-sql]
    A5[Ask APIs<br/>POST /ask<br/>POST /ask/stream]
    A6[Learning APIs<br/>POST /teach<br/>POST /teach/confirm<br/>POST /patterns/feedback<br/>GET /instructions<br/>DELETE /instructions/by-id]
    A7[Ops APIs<br/>GET /health<br/>GET /telemetry/recent<br/>GET /telemetry/summary<br/>GET /cache/stats<br/>POST /cache/clear<br/>POST and GET /benchmark/cases]
  end

  %% Core Services
  subgraph S[Core Services]
    S1[Help Docs Service<br/>OpenAPI metadata + curated route docs]
    S2[Ingest Service<br/>Chunking + embedding write pipeline]
    S3[Retrieve Service<br/>Rewrite + embedding + vector search + context]
    S4[ReAct Planner<br/>Action loop and control flow]
    S5[SQL Generator<br/>Guarded SQL drafting and validation]
    S6[Ask Orchestrator<br/>Generate -> Execute -> Answer]
    S7[Answer Generator<br/>Structured ANSWER/KEY FIGURES/DETAILS]
    S8[Instruction Store<br/>User rules and confidence updates]
    S9[Pattern Store<br/>Learned SQL patterns and feedback]
    S10[Cache Layer<br/>Embed cache + SQL cache]
    S11[Column Loader and MySQL Executor]
    S12[Telemetry and Benchmark Store Access]
  end

  %% Data Stores
  subgraph D[Data Layer]
    D1[(PostgreSQL + pgvector<br/>nl2sql_embeddings<br/>nl2sql_learned_patterns<br/>nl2sql_user_instructions<br/>nl2sql_request_events<br/>nl2sql_benchmark_cases)]
    D2[(MySQL App DB<br/>Business data and live schema columns)]
    D3[(RAG Sources<br/>rag_schema JSON + docs files)]
    D4[(In-Memory Runtime Caches)]
  end

  %% External Integrations
  subgraph E[External Integrations]
    E1[[Embedding API<br/>TEI-compatible]]
    E2[[Ollama LLM API<br/>Reasoning + SQL + Answer models]]
    E3[[systemd<br/>Service runtime manager]]
  end

  %% Client -> API
  C1 --> A0
  C2 --> A0
  C3 --> A0
  C4 --> A0

  %% API -> Service
  A0 --> A1
  A0 --> A2
  A0 --> A3
  A0 --> A4
  A0 --> A5
  A0 --> A6
  A0 --> A7

  A1 --> S1
  A2 --> S2
  A3 --> S3
  A4 --> S4
  A4 --> S5
  A5 --> S6
  A6 --> S8
  A6 --> S9
  A7 --> S10
  A7 --> S12

  %% Internal service interactions
  S3 --> S10
  S3 --> S4
  S4 --> S5
  S4 --> S3
  S6 --> S4
  S6 --> S11
  S6 --> S7
  S6 --> S8
  S6 --> S9

  %% Service -> Data
  S1 -. uses openapi .-> A0
  S2 --> D3
  S2 --> D1
  S3 --> D1
  S8 --> D1
  S9 --> D1
  S12 --> D1
  S10 <--> D4
  S11 <--> D2
  S4 --> D2

  %% Service -> External
  S2 --> E1
  S3 --> E1
  S3 -. optional query rewrite .-> E2
  S4 --> E2
  S5 --> E2
  S7 --> E2
  A0 -. runtime lifecycle .-> E3
```

## 2) API-to-Service Request Flow

```mermaid
sequenceDiagram
  autonumber
  participant U as Client
  participant API as FastAPI Router
  participant C as Cache
  participant R as Retrieval Service
  participant EMB as Embedding API
  participant PG as Postgres pgvector
  participant P as ReAct Planner
  participant LLM as Ollama
  participant MY as MySQL App DB
  participant A as Answer Generator
  participant T as Telemetry Store

  U->>API: POST /generate-sql or POST /ask

  API->>C: SQL cache lookup
  alt cache hit (status ok)
    C-->>API: cached SQL response
  else cache miss
    API->>R: retrieve_groups(query, top_k)
    R->>LLM: optional query rewrite
    R->>C: embed cache lookup
    alt embed cache miss
      R->>EMB: embed query
      EMB-->>R: vector
      R->>C: cache vector
    end
    R->>PG: vector similarity search
    PG-->>R: schema and knowledge context

    API->>P: start ReAct planning
    loop up to REACT_MAX_ITERATIONS
      P->>LLM: reasoning action request
      LLM-->>P: action + input
      alt action = GENERATE_SQL
        P->>LLM: SQL generation prompt
        LLM-->>P: SQL candidate
        P->>MY: EXPLAIN and schema validation checks
        MY-->>P: validation signals
      else action = RETRIEVE_MORE_CONTEXT
        P->>R: refined retrieval
      else action = ASK_CLARIFICATION or GIVE_UP
        P-->>API: clarification_needed
      end
    end
    P-->>API: ok or clarification_needed or rejected
    API->>C: write SQL cache on status ok
  end

  alt route is /ask and SQL status is ok
    API->>MY: execute bounded SELECT (max 50 rows)
    MY-->>API: rows + columns
    API->>A: build structured answer prompt
    A->>LLM: generate answer
    LLM-->>A: structured sections
    A-->>API: parsed answer + warnings
  end

  API->>T: persist request telemetry and stage latencies
  API-->>U: final response
```

## 3) Scalability Hooks for Future Changes

- Add endpoints by extending the API subgraph and linking to a new service node.
- Add new retrieval sources by attaching new nodes under Data Layer and wiring to Ingest or Retrieve.
- Add more models by extending the External Integrations subgraph and routing through model client abstraction.
- Add policy controls by inserting a governance service between ReAct Planner and SQL Generator.
- Keep cache and telemetry as shared cross-cutting components to avoid tight coupling.

## 4) Technology Stack Used

```mermaid
flowchart TB
  subgraph Runtime[Runtime and Service Layer]
    R1[Python 3.x]
    R2[FastAPI]
    R3[Uvicorn ASGI Server]
    R4[Pydantic Settings]
    R5[asyncio]
    R6[httpx Async Client]
  end

  subgraph Intelligence[Intelligence Layer]
    I1[Query Rewriter]
    I2[ReAct Planner]
    I3[SQL Generator]
    I4[Answer Generator]
    I5[Model Client Abstraction]
  end

  subgraph Data[Data and Retrieval Layer]
    D1[PostgreSQL]
    D2[pgvector]
    D3[asyncpg]
    D4[MySQL App DB]
    D5[RAG Schema JSON]
    D6[Docs and Knowledge Files]
    D7[In-Memory Embed and SQL Cache]
  end

  subgraph External[External Integrations]
    E1[Embedding API<br/>TEI-compatible]
    E2[Ollama API<br/>deepseek-coder and qwen3]
    E3[systemd]
  end

  subgraph DevOps[Tooling and Quality]
    O1[pytest]
    O2[Smoke Test Scripts]
    O3[Benchmark Replay Scripts]
    O4[Makefile Targets]
  end

  R1 --> R2
  R2 --> R3
  R2 --> R4
  R2 --> R5
  R2 --> R6

  R2 --> I1
  R2 --> I2
  R2 --> I3
  R2 --> I4
  I1 --> I5
  I2 --> I5
  I3 --> I5
  I4 --> I5

  R2 --> D3
  D3 --> D1
  D1 --> D2
  R2 --> D4
  R2 --> D5
  R2 --> D6
  R2 --> D7

  I5 --> E2
  I1 --> E2
  R2 --> E1
  R2 --> E3

  O1 --> R2
  O2 --> R2
  O3 --> R2
  O4 --> O2
  O4 --> O3
```

## 5) Stack Flow (Request to Response)

```mermaid
flowchart LR
  A[Client Request] --> B[FastAPI Endpoint]
  B --> C{Route Type}

  C -->|Ingest| D[Chunk and Prepare Content]
  D --> E[Call Embedding API]
  E --> F[Store Vectors in PostgreSQL pgvector]
  F --> Z[Return Ingest Summary]

  C -->|Query| G[Optional Query Rewrite]
  G --> H[Embed Query with Cache Check]
  H --> I[Vector Search in pgvector]
  I --> J[Build Retrieval Context]
  J --> Z

  C -->|Generate SQL| K[Retrieve Group Context]
  K --> L[ReAct Planning Loop]
  L --> M[Generate SQL via LLM]
  M --> N[Validate SQL Safety and Scope]
  N --> O{Valid SQL?}
  O -->|Yes| P[Optional SQL Cache Write]
  O -->|No| Q[Clarification or Rejection]
  P --> Z
  Q --> Z

  C -->|Ask| R[Run Generate SQL Path]
  R --> S{Generation Status}
  S -->|ok| T[Execute Bounded SQL on MySQL]
  T --> U[Generate Structured Answer]
  U --> V[Add Warnings if Needed]
  V --> W[Save Pattern and Update Instruction Outcomes]
  W --> Z
  S -->|clarification_needed or rejected| Z

  Z --> Y[Persist Telemetry Event]
  Y --> X[Final API Response]
```
