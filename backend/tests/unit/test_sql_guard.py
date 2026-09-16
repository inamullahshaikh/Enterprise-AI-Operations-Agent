import pytest

from relay_core.connectors.builtin.sql_guard import SQLGuardError, guard_select


def test_allows_a_plain_select_and_adds_a_limit() -> None:
    out = guard_select("SELECT * FROM accounts", allowed_schemas=["public"], row_limit=500)
    assert out == "SELECT * FROM accounts LIMIT 500"


def test_clamps_an_existing_limit_above_the_row_limit() -> None:
    out = guard_select(
        "SELECT * FROM accounts LIMIT 10000", allowed_schemas=["public"], row_limit=500
    )
    assert out == "SELECT * FROM accounts LIMIT 500"


def test_keeps_an_existing_limit_under_the_row_limit() -> None:
    out = guard_select("SELECT * FROM accounts LIMIT 10", allowed_schemas=["public"], row_limit=500)
    assert out == "SELECT * FROM accounts LIMIT 10"


def test_allows_a_with_clause_wrapping_a_select() -> None:
    out = guard_select(
        "WITH recent AS (SELECT * FROM accounts) SELECT * FROM recent",
        allowed_schemas=["public"],
        row_limit=500,
    )
    assert "SELECT" in out


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM accounts",
        "UPDATE accounts SET name = 'x'",
        "DROP TABLE accounts",
        "SELECT * FROM accounts; DELETE FROM accounts",
        "WITH d AS (DELETE FROM accounts RETURNING *) SELECT * FROM d",
        "SELECT pg_sleep(5)",
        "SELECT * FROM accounts INTO other_table",
    ],
)
def test_rejects_anything_that_isnt_a_plain_read_only_select(sql: str) -> None:
    with pytest.raises(SQLGuardError):
        guard_select(sql, allowed_schemas=["public"], row_limit=500)


def test_rejects_a_schema_outside_the_allow_list() -> None:
    with pytest.raises(SQLGuardError):
        guard_select(
            "SELECT * FROM other_schema.secrets", allowed_schemas=["public"], row_limit=500
        )


def test_allows_an_explicitly_qualified_allowed_schema() -> None:
    out = guard_select("SELECT * FROM public.accounts", allowed_schemas=["public"], row_limit=500)
    assert "public.accounts" in out
