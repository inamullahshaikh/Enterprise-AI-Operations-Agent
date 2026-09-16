"""Direct coverage of `relay_core.connectors.builtin.postgres.PostgresConnector`
(docs/system-design.md section 10.1) against a real Postgres database — the "customer"
database this connector queries, never Relay's own app database. `test_execute_step_flow.py`
already exercises this connector's happy-path `run_sql` end to end through the whole agent
graph, but nothing calls `list_tables`/`describe_table` directly or checks the blocked-query
and bad-credential paths, so those are the gap this file closes.

Uses its own module-scoped `PostgresContainer`, independent of `tests/integration/conftest.py`'s
session-scoped one (that container is Relay's own app database and gets migrated with Relay's
schema — this connector must never see that schema, only a plain "customer" table it's told
about via `config.schemas`).
"""

import uuid

import pytest
from testcontainers.community.postgres import PostgresContainer

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.postgres import PostgresConnector

pytestmark = pytest.mark.asyncio

_IDS = dict(
    workspace_id=uuid.uuid4(),
    user_id=uuid.uuid4(),
    run_id=uuid.uuid4(),
    conversation_id=uuid.uuid4(),
    installation_id="demo-db",
)


@pytest.fixture(scope="module")
def demo_pg():
    with PostgresContainer("postgres:16", username="acme", password="acme", dbname="acme") as pg:
        yield pg


@pytest.fixture
async def seeded(demo_pg: PostgresContainer) -> None:
    # Function-scoped (unlike the module-scoped container itself): pytest-asyncio's default
    # fixture loop scope is "function", so an async fixture above that scope would hit a
    # ScopeMismatch. Re-seeding per test is cheap and keeps tests independent of run order.
    import asyncpg

    conn = await asyncpg.connect(
        host=demo_pg.get_container_host_ip(),
        port=int(demo_pg.get_exposed_port(demo_pg.port)),
        database=demo_pg.dbname,
        user=demo_pg.username,
        password=demo_pg.password,
    )
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id serial PRIMARY KEY,
                name text NOT NULL,
                mrr numeric(10, 2) NOT NULL
            )
            """
        )
        await conn.execute("DELETE FROM accounts")
        await conn.execute(
            "INSERT INTO accounts (name, mrr) VALUES ('Acme Robotics', 1200), ('Globex', 800)"
        )
        await conn.execute("CREATE SCHEMA IF NOT EXISTS private")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS private.secrets (id serial PRIMARY KEY, value text)"
        )
    finally:
        await conn.close()


def _ctx(demo_pg: PostgresContainer, *, schemas: list[str] | None = None, row_limit: int = 500):
    return ExecutionContext(
        **_IDS,
        config={
            "host": demo_pg.get_container_host_ip(),
            "port": int(demo_pg.get_exposed_port(demo_pg.port)),
            "database": demo_pg.dbname,
            "schemas": schemas or ["public"],
            "statement_timeout_s": 10,
            "row_limit": row_limit,
        },
        secrets={"username": demo_pg.username, "password": demo_pg.password},
    )


async def test_list_tables_returns_the_accounts_table(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(_ctx(demo_pg), "list_tables", {})
    assert result.ok, result.error
    names = {row["table"] for row in result.content}
    assert "accounts" in names


async def test_describe_table_returns_columns_and_sample_rows(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(_ctx(demo_pg), "describe_table", {"table": "accounts"})
    assert result.ok, result.error
    column_names = {c["column_name"] for c in result.content["columns"]}
    assert column_names == {"id", "name", "mrr"}
    assert result.content["primary_key"] == ["id"]
    assert len(result.content["sample_rows"]) <= 3


async def test_describe_table_missing_table_is_a_tool_error_not_a_raise(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(_ctx(demo_pg), "describe_table", {"table": "no_such_table"})
    assert result.ok is False
    assert "not found" in (result.error or "")


async def test_describe_table_rejects_a_schema_outside_the_allow_list(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(
        _ctx(demo_pg, schemas=["public"]),
        "describe_table",
        {"schema": "private", "table": "secrets"},
    )
    assert result.ok is False
    assert "not in this connector's allowed" in (result.error or "")


async def test_run_sql_executes_a_select_against_real_data(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(
        _ctx(demo_pg), "run_sql", {"sql": "SELECT name, mrr FROM accounts ORDER BY mrr DESC"}
    )
    assert result.ok, result.error
    assert [row["name"] for row in result.content] == ["Acme Robotics", "Globex"]
    assert result.meta["row_count"] == 2


async def test_run_sql_blocks_a_write_statement(demo_pg: PostgresContainer, seeded: None) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(_ctx(demo_pg), "run_sql", {"sql": "DELETE FROM accounts"})
    assert result.ok is False
    assert "SELECT" in (result.error or "")

    # The guard's rejection actually stopped the statement — the table is untouched.
    verify = await connector.call_tool(_ctx(demo_pg), "run_sql", {"sql": "SELECT * FROM accounts"})
    assert len(verify.content) == 2


async def test_run_sql_blocks_a_schema_outside_the_allow_list(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(
        _ctx(demo_pg, schemas=["public"]),
        "run_sql",
        {"sql": "SELECT * FROM private.secrets"},
    )
    assert result.ok is False
    assert "not in this connector's allowed schema list" in (result.error or "")


async def test_run_sql_clamps_the_row_limit(demo_pg: PostgresContainer, seeded: None) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(
        _ctx(demo_pg, row_limit=1), "run_sql", {"sql": "SELECT * FROM accounts"}
    )
    assert result.ok, result.error
    assert len(result.content) == 1
    assert result.truncated is True


async def test_unknown_tool_name_is_a_tool_error(demo_pg: PostgresContainer, seeded: None) -> None:
    connector = PostgresConnector()
    result = await connector.call_tool(_ctx(demo_pg), "delete_everything", {})
    assert result.ok is False
    assert "Unknown tool" in (result.error or "")


async def test_health_check_succeeds_with_valid_credentials(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    healthy, message = await connector.health_check(_ctx(demo_pg))
    assert healthy is True
    assert message == "Connected"


async def test_health_check_fails_with_bad_credentials(
    demo_pg: PostgresContainer, seeded: None
) -> None:
    connector = PostgresConnector()
    ctx = _ctx(demo_pg)
    bad_ctx = ctx.model_copy(update={"secrets": {"username": "acme", "password": "wrong"}})
    healthy, message = await connector.health_check(bad_ctx)
    assert healthy is False
    assert "Could not connect" in message
