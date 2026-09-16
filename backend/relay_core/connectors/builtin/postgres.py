"""The `postgres` connector (docs/system-design.md section 10.1): read-only analytics over a
company's own Postgres database, never Relay's. Every call opens and closes its own
`asyncpg` connection rather than keeping a pool per installation — simple, and fine at the
call volumes this phase runs at; worth revisiting if per-call connection overhead ever shows
up in the p95 latency budget (section 2.2).
"""

import re
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from pydantic import BaseModel

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.connectors.builtin.sql_guard import SQLGuardError, guard_select

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# All three tools share one capability list rather than splitting `sql.query` (schema
# inspection) from `customer.read`/`subscription.read`/`usage.read` (data): a plan step that
# requires `subscription.read` still needs `list_tables`/`describe_table` in its tool list to
# inspect the schema before calling `run_sql` (docs/system-design.md section 8.8's executor
# prompt), and `relay_core.tools.registry.ToolRegistry` filters tools by exact capability
# overlap — splitting the list would filter the inspection tools out of exactly the steps that
# need them most.
_CAPABILITIES = ["sql.query", "customer.read", "subscription.read", "usage.read"]


class _Config(BaseModel):
    host: str
    port: int = 5432
    database: str
    schemas: list[str] = ["public"]
    statement_timeout_s: float = 10.0
    row_limit: int = 500


class _Secrets(BaseModel):
    username: str
    password: str


def _quote_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise SQLGuardError(f"Invalid identifier {name!r}")
    return f'"{name}"'


class PostgresConnector(Connector):
    key = "postgres"
    display_name = "PostgreSQL"
    auth_type = AuthType.CONNECTION_STRING

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_tables",
                description=(
                    "List tables and approximate row counts in this database's allowed schemas."
                ),
                input_schema={"type": "object", "properties": {}},
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
            ToolSpec(
                name="describe_table",
                description=(
                    "Columns, types, primary/foreign keys, and a 3-row sample for one table."
                ),
                input_schema={
                    "type": "object",
                    "required": ["table"],
                    "properties": {
                        "schema": {"type": "string", "description": "Defaults to 'public'."},
                        "table": {"type": "string"},
                    },
                },
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
            ToolSpec(
                name="run_sql",
                description=(
                    "Execute one read-only SELECT statement. Call list_tables/describe_table "
                    "first to find the right tables and columns. A LIMIT is added "
                    "automatically if your query doesn't include one."
                ),
                input_schema={
                    "type": "object",
                    "required": ["sql"],
                    "properties": {"sql": {"type": "string"}},
                },
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        config = _Config.model_validate(ctx.config)
        secrets = _Secrets.model_validate(ctx.secrets)
        try:
            if tool_name == "list_tables":
                return await self._list_tables(config, secrets)
            if tool_name == "describe_table":
                return await self._describe_table(config, secrets, args)
            if tool_name == "run_sql":
                return await self._run_sql(config, secrets, args)
            return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")
        except SQLGuardError as exc:
            return ToolResult(ok=False, error=str(exc))
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            return ToolResult(ok=False, error=f"Database error: {exc}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        config = _Config.model_validate(ctx.config)
        secrets = _Secrets.model_validate(ctx.secrets)
        try:
            conn = await self._connect(config, secrets)
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            return False, f"Could not connect: {exc}"
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
        return True, "Connected"

    async def _connect(self, config: _Config, secrets: _Secrets) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=config.host,
            port=config.port,
            database=config.database,
            user=secrets.username,
            password=secrets.password,
            timeout=5,
        )

    async def _list_tables(self, config: _Config, secrets: _Secrets) -> ToolResult:
        conn = await self._connect(config, secrets)
        try:
            rows = await conn.fetch(
                """
                SELECT n.nspname AS schema, c.relname AS table,
                       GREATEST(c.reltuples, 0)::bigint AS approx_row_count
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r' AND n.nspname = ANY($1::text[])
                ORDER BY n.nspname, c.relname
                """,
                config.schemas,
            )
        finally:
            await conn.close()
        return ToolResult(ok=True, content=[dict(r) for r in rows])

    async def _describe_table(
        self, config: _Config, secrets: _Secrets, args: dict[str, Any]
    ) -> ToolResult:
        schema_name = args.get("schema") or "public"
        table_name = args.get("table") or ""
        if schema_name not in config.schemas:
            return ToolResult(
                ok=False,
                error=(
                    f"Schema {schema_name!r} is not in this connector's allowed list "
                    f"{config.schemas}"
                ),
            )
        quoted_schema, quoted_table = _quote_ident(schema_name), _quote_ident(table_name)

        conn = await self._connect(config, secrets)
        try:
            columns = await conn.fetch(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = $1 AND table_name = $2
                   ORDER BY ordinal_position""",
                schema_name,
                table_name,
            )
            if not columns:
                return ToolResult(ok=False, error=f"Table {schema_name}.{table_name} not found")
            primary_key = await conn.fetch(
                """SELECT kcu.column_name FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                   WHERE tc.table_schema = $1 AND tc.table_name = $2
                     AND tc.constraint_type = 'PRIMARY KEY'""",
                schema_name,
                table_name,
            )
            foreign_keys = await conn.fetch(
                """SELECT kcu.column_name, ccu.table_schema AS foreign_schema,
                          ccu.table_name AS foreign_table, ccu.column_name AS foreign_column
                   FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                   JOIN information_schema.constraint_column_usage ccu
                     ON tc.constraint_name = ccu.constraint_name
                    AND tc.table_schema = ccu.table_schema
                   WHERE tc.table_schema = $1 AND tc.table_name = $2
                     AND tc.constraint_type = 'FOREIGN KEY'""",
                schema_name,
                table_name,
            )
            sample_rows = await conn.fetch(f"SELECT * FROM {quoted_schema}.{quoted_table} LIMIT 3")
        finally:
            await conn.close()
        return ToolResult(
            ok=True,
            content={
                "columns": [dict(c) for c in columns],
                "primary_key": [r["column_name"] for r in primary_key],
                "foreign_keys": [dict(r) for r in foreign_keys],
                "sample_rows": [dict(r) for r in sample_rows],
            },
        )

    async def _run_sql(
        self, config: _Config, secrets: _Secrets, args: dict[str, Any]
    ) -> ToolResult:
        sql = args.get("sql")
        if not sql or not isinstance(sql, str):
            return ToolResult(ok=False, error="'sql' is required")
        safe_sql = guard_select(sql, allowed_schemas=config.schemas, row_limit=config.row_limit)

        conn = await self._connect(config, secrets)
        try:
            async with conn.transaction(readonly=True):
                timeout_ms = int(config.statement_timeout_s * 1000)
                await conn.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
                rows = await conn.fetch(safe_sql)
        finally:
            await conn.close()
        return ToolResult(
            ok=True,
            content=[dict(r) for r in rows],
            truncated=len(rows) >= config.row_limit,
            meta={"row_count": len(rows), "sql_executed": safe_sql},
        )
