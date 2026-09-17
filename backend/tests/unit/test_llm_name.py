"""`relay_core.tools.sync.llm_name`: every result is a valid Gemini function name, and names that
sanitize or truncate to the same text stay distinct."""

import re

from relay_core.tools.sync import llm_name

_VALID = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")


def test_a_valid_name_is_left_alone() -> None:
    assert llm_name("sales-db", "run_sql") == "sales-db__run_sql"


def test_disallowed_characters_are_replaced_without_colliding() -> None:
    sanitized = llm_name("tickets", "search tickets/v2")
    assert _VALID.fullmatch(sanitized)
    assert sanitized.startswith("tickets__search_tickets_v2_")
    assert sanitized != llm_name("tickets", "search_tickets_v2")


def test_a_leading_digit_gets_an_underscore() -> None:
    name = llm_name("3m-db", "run_sql")
    assert _VALID.fullmatch(name)
    assert name.startswith("_3m-db__run_sql")


def test_long_names_are_truncated_to_64_and_stay_unique() -> None:
    a = llm_name("tickets", "x" * 80 + "a")
    b = llm_name("tickets", "x" * 80 + "b")
    assert len(a) == len(b) == 64
    assert _VALID.fullmatch(a) and _VALID.fullmatch(b)
    assert a != b
