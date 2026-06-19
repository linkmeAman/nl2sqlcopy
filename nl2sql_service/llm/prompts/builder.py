from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    body: str

    def render(self, values: Mapping[str, object]) -> str:
        rendered = self.body
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered.strip()


class PromptBuilder:
    _templates: dict[str, PromptTemplate] = {
        "sql_generation": PromptTemplate(
            name="sql_generation",
            body="""
You are an NL2SQL generator for a {dialect} database.
Generate one read-only SELECT/WITH query.

User question:
{user_query}

Schema context:
{schema}

Retrieved context:
{context}

Return only SQL.
""",
        ),
        "query_rewrite": PromptTemplate(
            name="query_rewrite",
            body="""
Rewrite the user question for embedding retrieval.
Preserve literals and add useful schema/business terms.

Hints:
{hints}

User question:
{user_query}

Return JSON only: {"search_query": "..."}
""",
        ),
        "answer": PromptTemplate(
            name="answer",
            body="""
Answer using only the supplied data.

Question:
{user_query}

Data:
{context}
""",
        ),
    }

    @classmethod
    def build(cls, task: str, **values: object) -> str:
        template = cls._templates.get(task)
        if template is None:
            raise ValueError(f"Unknown prompt task: {task}")
        return template.render(values)

    @classmethod
    def register(cls, task: str, body: str) -> None:
        cls._templates[task] = PromptTemplate(name=task, body=body)
