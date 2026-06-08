# NL2SQL Benchmarks

The evaluation CLI loads these suite files directly:

- `level1_basic.json`
- `level2_intermediate.json`
- `level3_advanced.json`
- `level4_expert.json`
- `level5_stress.json`

Each file is a `BenchmarkSuite` object with:

- `suite_id`
- `level`
- `title`
- `description`
- `cases`

Each case contains:

- `id`
- `query`
- `expected_criteria`
- `expected_tables`
- `expected_keywords`
- `expected_sql_characteristics`
- `failure_classification_hints`
- optional `endpoint`, `top_k`, `top_k_values`, `repeat`, and `metadata`

The evaluation runner can also sync these cases into the backend via `POST /benchmark/cases`
when `--sync-db` is enabled.

Suite levels map to the evaluation tiers used by the CLI:

- level 1: basic retrieval and single-table matching
- level 2: intermediate semantic and ambiguity cases
- level 3: advanced multi-table reasoning
- level 4: expert conflict and hallucination traps
- level 5: stress tests for top-k, reranking, and cache behavior
