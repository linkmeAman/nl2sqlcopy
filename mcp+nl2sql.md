# MCP + NL2SQL Production Architecture Guide

## Author

Aman Singh

## Version

1.0

---

# Table of Contents

1. Introduction
2. What is MCP
3. Why MCP Matters
4. End-to-End Request Flow
5. High Level Architecture
6. MCP Server Components
7. Query Lifecycle
8. Security Architecture
9. SQL Validation Layer
10. Multi-Tenant Design
11. Data Access Control
12. MCP Tool Design
13. Caching Strategy
14. RAG Integration
15. Logging & Observability
16. FastAPI Integration
17. Deployment Architecture
18. Scalability Considerations
19. Authentication & Rate Limiting
20. Production Checklist
21. Sample Folder Structure
22. Future Enhancements
23. Final Principle

---

# Introduction

This document describes a production-grade architecture for building an MCP-powered NL2SQL platform.

The goal is to allow Large Language Models (LLMs) to answer business questions using enterprise databases while maintaining:

* Security
* Reliability
* Performance
* Multi-Tenant Isolation
* Observability
* Vendor Independence

The architecture is designed around one key principle:

> The LLM can generate SQL, but the platform is responsible for ensuring that SQL is safe, valid, and compliant before execution.

---

# What is MCP

Model Context Protocol (MCP) is a standardized protocol that allows AI models to interact with external systems through a common interface.

Think of it as:

```
REST API  → Applications
MCP       → AI Agents
```

Without MCP, every AI provider requires its own integration:

```
OpenAI  → Database
Claude  → Database
Gemini  → Database
```

With MCP:

```
AI Model
    │
    ▼
MCP Client
    │
    ▼
MCP Server
    │
    ▼
Tools & Data Sources
```

A single MCP implementation can be reused across multiple AI providers and agent frameworks.

---

# Why MCP Matters

Traditional AI systems often create vendor lock-in because each model requires custom tool integrations.

Example:

```
OpenAI  → MySQL
Claude  → MySQL
Gemini  → MySQL
```

This results in duplicated development effort.

With MCP:

```
OpenAI
Claude
Gemini
Cursor
VS Code
    │
    ▼
MCP Server
    │
    ▼
MySQL
```

Benefits:

* One integration layer
* Easier maintenance
* Vendor independence
* Consistent security controls
* Reusable tools

---

# End-to-End Request Flow

The easiest way to understand the system is to follow a user request from start to finish.

### User Question

```
"Show top 10 branches by revenue this month"
```

### Processing Flow

```
User
  │
  ▼
FastAPI API
  │
  ▼
Agent Layer
  │
  ▼
MCP Client
  │
  ▼
MCP Server
  │
  ├── Get Schema
  ├── Get Business Rules
  ├── Generate SQL
  ├── Validate SQL
  ├── Apply Tenant Filters
  ├── Check Query Cost
  └── Execute Query
  │
  ▼
MySQL
  │
  ▼
Results
  │
  ▼
LLM Formats Response
  │
  ▼
User
```

This flow ensures that SQL is never executed directly after generation.

---

# High Level Architecture

```
┌─────────────┐
│    User     │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   FastAPI   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Agent Layer │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ MCP Client  │
└──────┬──────┘
       │
       ▼
┌─────────────────────┐
│     MCP Server      │
└──────┬──────┬────────┘
       │      │
       ▼      ▼
   Redis     MySQL
       │
       ▼
Business Rules
Schema Metadata
```

---

# MCP Server Components

The MCP Server acts as the central orchestration layer.

Its responsibility is to expose secure tools to the AI agent while protecting backend systems.

---

## Schema Service

Provides database structure information.

Responsibilities:

* Table metadata
* Column metadata
* Relationships
* Foreign keys

Tool:

```python
get_schema()
```

Example Response:

```json
{
  "students": [
    "id",
    "name",
    "branch"
  ]
}
```

---

## Business Rule Service

Stores domain-specific knowledge that may not exist in the database schema.

Examples:

* Active students = status = 1
* Revenue = paid_amount
* Enrollment date = created_at

Tool:

```python
get_business_rules()
```

---

## Query Generator

Uses:

* User question
* Schema metadata
* Business rules

To generate SQL.

Example:

Input:

```
Show top branches by revenue
```

Output:

```sql
SELECT
    branch,
    SUM(paid_amount) AS revenue
FROM students
GROUP BY branch;
```

---

## SQL Validator

The most critical component in the system.

Responsibilities:

* Parse SQL
* Validate tables
* Validate columns
* Enforce policies
* Block dangerous statements

Allowed:

```sql
SELECT
```

Blocked:

```sql
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
```

Recommended Library:

```
sqlglot
```

---

# Query Lifecycle

Every query should pass through the following stages:

```
Natural Language Question
            │
            ▼
      Generate SQL
            │
            ▼
      Validate SQL
            │
            ▼
 Apply Security Rules
            │
            ▼
 Apply Tenant Filters
            │
            ▼
     Cost Analysis
            │
            ▼
      Execute Query
            │
            ▼
      Format Results
            │
            ▼
      Return Response
```

This pipeline prevents unsafe SQL from reaching the database.

---

# Security Architecture

## Core Principle

Never trust the LLM.

Assume the model may generate:

* Invalid SQL
* Hallucinated tables
* Hallucinated columns
* Expensive queries
* Security violations

The platform must enforce safety independently of the model.

---

# SQL Validation Layer

Never execute raw SQL generated by an LLM.

Validation Pipeline:

```
Generated SQL
      │
      ▼
SQL Parser
      │
      ▼
Security Validation
      │
      ▼
Tenant Enforcement
      │
      ▼
Cost Analysis
      │
      ▼
Execution
```

---

# Query Cost Analysis

A valid query can still be dangerous.

Example:

```sql
SELECT *
FROM leads;
```

If the table contains 200 million rows, the query may overload the database.

Recommended Rules:

```text
MAX_ROWS = 1000
MAX_EXECUTION_TIME = 5 seconds
```

Enforce:

* LIMIT clause
* No cartesian joins
* No unrestricted scans
* Query timeout limits

---

# Multi-Tenant Design

Every request must include:

```text
tenant_id
```

Example:

```text
tenant_id = 123
```

Generated SQL:

```sql
SELECT *
FROM students;
```

Server-Enforced SQL:

```sql
SELECT *
FROM students
WHERE tenant_id = 123;
```

Important:

Tenant filtering must be applied by the server, not by the LLM.

---

# Data Access Control

Not every column should be accessible.

Example Table:

```text
users
├── id
├── email
├── password_hash
└── salary
```

Allowed:

```text
id
email
```

Blocked:

```text
password_hash
salary
```

Implement:

* Column-level permissions
* Role-based access control
* Sensitive data masking

---

# MCP Tool Design

Avoid exposing large, unrestricted tools.

Bad:

```python
execute_sql(query)
```

Better:

```python
get_schema()
get_business_rules()
generate_sql()
validate_sql()
execute_query()
format_results()
```

Benefits:

* Easier auditing
* Better security
* Fine-grained permissions
* Improved observability

---

# Recommended MCP Tools

## Tool: get_schema

Purpose:

Retrieve database schema metadata.

---

## Tool: get_business_rules

Purpose:

Retrieve domain-specific business rules.

---

## Tool: generate_sql

Purpose:

Convert natural language into SQL.

---

## Tool: validate_sql

Purpose:

Validate generated SQL before execution.

---

## Tool: explain_query

Purpose:

Explain query logic to users.

---

## Tool: execute_query

Purpose:

Execute validated SQL.

---

## Tool: export_csv

Purpose:

Export query results.

---

# Caching Strategy

Redis should be used to reduce latency and database load.

### Schema Metadata

TTL:

```text
1 Hour
```

### Business Rules

TTL:

```text
6 Hours
```

### Frequently Executed Queries

TTL:

```text
15 Minutes
```

Benefits:

* Faster responses
* Reduced database load
* Lower token consumption
* Improved scalability

---

# RAG Integration

Instead of sending only the user question to the LLM, enrich the prompt with relevant context.

Recommended Flow:

```
User Question
       │
       ▼
   Retriever
       │
       ├── Schema Metadata
       ├── Business Rules
       └── Examples
       │
       ▼
       LLM
       │
       ▼
       SQL
```

Benefits:

* Better SQL generation
* Fewer hallucinations
* Improved accuracy

---

# Logging & Observability

## Logging

Capture:

* User Query
* Generated SQL
* Tool Calls
* Token Usage
* Execution Time
* Result Count

Example:

```json
{
  "user_id": 101,
  "tenant_id": 123,
  "question": "Show revenue",
  "generated_sql": "...",
  "execution_time": 230,
  "rows": 50
}
```

---

## Observability

Track:

* Request Count
* Tool Calls
* SQL Latency
* Error Rate
* Cache Hit Ratio
* Token Usage

Recommended Tools:

* Prometheus
* Grafana
* OpenTelemetry
* Langfuse

---

# FastAPI Integration

Recommended Architecture:

```
FastAPI
   │
   ▼
Agent Layer
   │
   ▼
MCP Client
   │
   ▼
MCP Server
```

Responsibilities:

### FastAPI

* Authentication
* Request handling
* Response delivery

### Agent Layer

* Reasoning
* Tool orchestration

### MCP Server

* Data access
* Validation
* Security enforcement

FastAPI should never directly generate or execute SQL.

---

# Deployment Architecture

```
                    Kubernetes
                         │
                         ▼

┌──────────────────────────────┐
│         FastAPI Pods         │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│        MCP Server Pods       │
└──────────────┬───────────────┘
               │
      ┌────────┴────────┐
      ▼                 ▼

   Redis             MySQL
```

---

# Scalability Considerations

### Horizontal Scaling

Scale:

* FastAPI Pods
* MCP Server Pods

### Stateless Services

Avoid:

* In-memory sessions
* Local caches

Use:

```text
Redis
```

For:

* Shared cache
* Session storage
* Distributed coordination

---

# Authentication & Rate Limiting

## Authentication

Recommended:

```text
JWT
```

Flow:

```
User
  │
  ▼
JWT Token
  │
  ▼
FastAPI
  │
  ▼
MCP Request
```

Extract:

* user_id
* tenant_id
* role

---

## Rate Limiting

Protect:

* LLM calls
* MCP tools
* Database execution

Example:

```text
100 requests/minute per tenant
```

This prevents abuse and protects infrastructure.

---

# Production Checklist

## Security

* [ ] Tenant Isolation
* [ ] SQL Validation
* [ ] Column Access Control
* [ ] Rate Limiting
* [ ] Audit Logs

## Performance

* [ ] Redis Cache
* [ ] Connection Pooling
* [ ] Query Optimization
* [ ] Monitoring

## Reliability

* [ ] Retries
* [ ] Circuit Breakers
* [ ] Health Checks
* [ ] Backup Strategy

---

# Sample Folder Structure

```text
project/
├── api/
├── agent/
├── mcp_server/
├── database/
├── redis/
├── monitoring/
├── deployment/
├── tests/
└── docs/
```

---

# Future Enhancements

1. Agentic Query Planning
2. Autonomous Schema Discovery
3. Semantic Query Caching
4. Multi-Database Support
5. PostgreSQL Support
6. BigQuery Support
7. Snowflake Support
8. Query Cost Prediction
9. Human Approval Workflows
10. Fine-Tuned SQL Models

---

# Final Principle

Generating SQL is relatively easy.

Executing AI-generated SQL safely in a production environment is the real challenge.

Successful AI systems do not rely on the model being correct. They assume the model can fail and build multiple layers of validation, security, and governance around it.

**The model generates SQL. The platform decides whether that SQL is allowed to run.**
