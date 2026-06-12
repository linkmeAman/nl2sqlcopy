from __future__ import annotations

from nl2sql_service import instruction_store


def test_instruction_query_filter_rejects_unrelated_contact_correction() -> None:
    instruction = {
        "instruction_type": "correction",
        "content": (
            "In here contact represents contact table in actuallity. "
            "Name represent the columns like fname,lname in the table"
        ),
        "tables_affected": ["contact"],
        "source_query": "get contact info with name aman singh",
    }

    assert instruction_store._instruction_matches_query("latest payment", instruction) is False


def test_instruction_query_filter_allows_matching_term_mapping() -> None:
    instruction = {
        "instruction_type": "term_mapping",
        "content": "counselor means employee table",
        "tables_affected": ["employee"],
        "source_query": None,
    }

    assert instruction_store._instruction_matches_query("show counselor sales", instruction) is True


def test_instruction_query_filter_allows_same_intent_variant() -> None:
    instruction = {
        "instruction_type": "correction",
        "content": "Use payment table for latest payment lookups.",
        "tables_affected": ["payment"],
        "source_query": "latest payment",
    }

    assert instruction_store._instruction_matches_query("newest payment", instruction) is True
