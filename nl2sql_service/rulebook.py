from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    enabled: bool
    category: str
    severity: str
    instruction: str


RULES: list[Rule] = [
    Rule(
        name="schema_fidelity",
        description="Only use tables and columns explicitly present in scope and column context.",
        enabled=True,
        category="boundary",
        severity="hard",
        instruction="""
SCHEMA FIDELITY (HARD RULE):
Only use tables and columns that are explicitly listed
in the provided TABLE_COLUMNS and TABLES IN SCOPE.
Never infer, guess, or assume that a column or table
exists. If a column is not listed, do not use it.
If a table is not listed, do not use it.
Violation = immediate rejection.""".strip(),
    ),
    Rule(
        name="query_safety",
        description="Generate read-only SQL only, with no destructive or mutating statements.",
        enabled=True,
        category="safety",
        severity="hard",
        instruction="""
QUERY SAFETY (HARD RULE):
You may only generate SELECT or WITH...SELECT statements.
You must NEVER generate:
INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE,
ALTER, GRANT, REVOKE, EXEC, EXECUTE.
Not even as a comment, not even as an example.
This rule has no exceptions.""".strip(),
    ),
    Rule(
        name="scope_boundary",
        description="Only reference tables that are explicitly allowed for the current request.",
        enabled=True,
        category="boundary",
        severity="hard",
        instruction="""
SCOPE BOUNDARY (HARD RULE):
Only query tables listed in TABLES IN SCOPE.
Do not JOIN, SUBQUERY, or reference any table
not explicitly listed.
Schema-qualified names (db.table) must still
match the bare table name in TABLES IN SCOPE.""".strip(),
    ),
    Rule(
        name="single_statement",
        description="Return exactly one SQL statement.",
        enabled=True,
        category="safety",
        severity="hard",
        instruction="""
SINGLE STATEMENT (HARD RULE):
Return exactly ONE SQL statement.
No semicolons between statements.
No multiple queries.
One SELECT. One result.""".strip(),
    ),
    Rule(
        name="answer_grounding",
        description="Ground every answer detail directly in the returned result rows.",
        enabled=True,
        category="quality",
        severity="hard",
        instruction="""
ANSWER GROUNDING (HARD RULE):
Every number, name, date, or amount in your answer
must come directly from the data rows provided.
Do not use your training knowledge to fill in gaps.
Do not estimate, approximate, or extrapolate.
If the data does not contain the answer, say:
"The data does not show this information." """.strip(),
    ),
    Rule(
        name="uncertainty_declaration",
        description="Prefer clarification or refusal over guessed schema or business logic.",
        enabled=True,
        category="behavior",
        severity="hard",
        instruction="""
UNCERTAINTY DECLARATION (HARD RULE):
If you are not confident about a table name,
column name, join condition, or business rule:
DO NOT GUESS.
Instead, choose ASK_CLARIFICATION or GIVE_UP.
A wrong SQL that looks right is worse than
an honest "I don't know." """.strip(),
    ),
    Rule(
        name="self_verification",
        description="Run a final self-check before returning a supposedly valid SQL query.",
        enabled=True,
        category="verification",
        severity="soft",
        instruction="""
SELF-VERIFICATION (REQUIRED BEFORE RETURNING):
Before choosing VALIDATE_AND_RETURN, ask yourself:
1. Does this SQL actually answer the question asked?
2. Are all columns in SELECT relevant to the question?
3. Is the WHERE clause correct for the filter requested?
4. If the user asked for "recent" items, is there
   an ORDER BY with a date column?
5. Would this SQL return zero rows if run? If yes,
   reconsider the WHERE conditions.
Only choose VALIDATE_AND_RETURN if all 5 checks pass.""".strip(),
    ),
    Rule(
        name="column_selection_quality",
        description="Choose concise, relevant columns unless the user explicitly asks for full detail.",
        enabled=True,
        category="quality",
        severity="soft",
        instruction="""
COLUMN SELECTION QUALITY (SOFT RULE):
For listing queries (show, list, find, fetch, get):
  SELECT only: id, name/title/subject, status, date.
  Do NOT select: amount, balance, audit columns,
  internal flags, unless specifically asked.
For aggregation queries (total, count, sum, average):
  SELECT only the columns needed for the calculation.
For detail queries (details, full, complete, all):
  SELECT * is acceptable.""".strip(),
    ),
    Rule(
        name="no_assumptions",
        description="Avoid guessing ambiguous column meaning, status semantics, or date fields.",
        enabled=True,
        category="behavior",
        severity="soft",
        instruction="""
NO ASSUMPTIONS (SOFT RULE):
Do not assume the meaning of ambiguous column names.
Do not assume which status values mean "active" or
"inactive" unless told in USER-PROVIDED RULES.
Do not assume date column names (use only what is
listed in TABLE_COLUMNS).
If multiple columns could match, choose the most
obviously correct one or ASK_CLARIFICATION.""".strip(),
    ),
    Rule(
        name="user_rules_priority",
        description="Treat injected user-provided rules as authoritative for this deployment.",
        enabled=True,
        category="behavior",
        severity="hard",
        instruction="""
USER RULES PRIORITY (HARD RULE):
If USER-PROVIDED RULES appear in the context,
they override your defaults completely.
Follow them exactly, even if they seem unusual.
User-provided join conditions, filters, and term
mappings are ground truth for this deployment.
Do not substitute your own judgment.""".strip(),
    ),
]

_ALL_RULE_NAMES = [rule.name for rule in RULES]
_CATEGORY_ORDER = {
    "safety": 0,
    "boundary": 1,
    "behavior": 2,
    "quality": 3,
    "verification": 4,
}
_RULE_INDEX = {rule.name: index for index, rule in enumerate(RULES)}
_CONTEXT_RULES = {
    "react": set(_ALL_RULE_NAMES),
    "sql_gen": {
        "schema_fidelity",
        "query_safety",
        "scope_boundary",
        "single_statement",
        "self_verification",
        "column_selection_quality",
        "no_assumptions",
        "user_rules_priority",
    },
    "answer": {
        "answer_grounding",
        "no_assumptions",
    },
    "clarification": {
        "uncertainty_declaration",
    },
}


@dataclass(frozen=True)
class RulebookConfig:
    enabled_rules: list[str] = field(default_factory=lambda: list(_ALL_RULE_NAMES))
    inject_in_react: bool = True
    inject_in_sql_gen: bool = True
    inject_in_answer: bool = True
    inject_in_clarification: bool = False


def get_active_rules(
    config: RulebookConfig,
) -> list[Rule]:
    enabled_lookup = {name.strip() for name in config.enabled_rules if name.strip()}
    active = [
        rule
        for rule in RULES
        if rule.enabled and rule.name in enabled_lookup
    ]
    return sorted(
        active,
        key=lambda rule: (_CATEGORY_ORDER.get(rule.category, 999), _RULE_INDEX[rule.name]),
    )


def build_governance_block(
    config: RulebookConfig,
    context: str = "react",
) -> str:
    if context == "react" and not config.inject_in_react:
        return ""
    if context == "sql_gen" and not config.inject_in_sql_gen:
        return ""
    if context == "answer" and not config.inject_in_answer:
        return ""
    if context == "clarification" and not config.inject_in_clarification:
        return ""

    allowed_names = _CONTEXT_RULES.get(context)
    if not allowed_names:
        return ""

    rules = [rule for rule in get_active_rules(config) if rule.name in allowed_names]
    if not rules:
        return ""

    rendered = "\n\n".join(rule.instruction for rule in rules)
    return (
        "=== GOVERNANCE RULES (follow these strictly) ===\n"
        f"{rendered}\n"
        "=== END GOVERNANCE RULES ==="
    )


def load_config_from_settings(
    settings: Any,
) -> RulebookConfig:
    governance_enabled = bool(getattr(settings, "governance_enabled", True))
    raw_enabled_rules = str(getattr(settings, "governance_enabled_rules", "all") or "all")

    if not governance_enabled:
        enabled_rules: list[str] = []
    elif raw_enabled_rules.strip().lower() == "all":
        enabled_rules = list(_ALL_RULE_NAMES)
    else:
        requested = [part.strip() for part in raw_enabled_rules.split(",")]
        enabled_rules = [name for name in requested if name in _RULE_INDEX]

    return RulebookConfig(
        enabled_rules=enabled_rules,
        inject_in_react=bool(getattr(settings, "governance_inject_react", True)),
        inject_in_sql_gen=bool(getattr(settings, "governance_inject_sql", True)),
        inject_in_answer=bool(getattr(settings, "governance_inject_answer", True)),
        inject_in_clarification=bool(
            getattr(settings, "governance_inject_clarification", False)
        ),
    )


_config: RulebookConfig | None = None


def get_config(settings: Any) -> RulebookConfig:
    global _config
    loaded = load_config_from_settings(settings)
    if _config is None or _config != loaded:
        _config = loaded
    return _config
