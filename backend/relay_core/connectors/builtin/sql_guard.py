"""The SQL safety pipeline for the `postgres` connector (docs/system-design.md section 10.1,
steps 1-4; steps 5-6 — running inside `BEGIN READ ONLY` with a statement timeout, and PII
masking — are the connector's and a later phase's job respectively, not this module's).

This is intentionally a second, independent layer of defense on top of the connector always
connecting as a read-only database role: `sqlglot`'s AST gives a much stronger guarantee than
string matching (e.g. it catches a data-modifying CTE like
`WITH d AS (DELETE FROM accounts RETURNING *) SELECT * FROM d`, which no regex-based blocklist
would reliably catch), but the read-only role is what actually stops a bypass here from doing
damage.
"""

import sqlglot
from sqlglot import exp

_DENIED_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "dblink",
    "dblink_connect",
    "dblink_exec",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "set_config",
    "pg_reload_conf",
}


class SQLGuardError(Exception):
    pass


def guard_select(sql: str, *, allowed_schemas: list[str], row_limit: int) -> str:
    """Validates `sql` is a single read-only `SELECT`/`WITH ... SELECT` against an allowed
    schema list, clamps its `LIMIT`, and returns the (possibly rewritten) SQL to execute.
    Raises `SQLGuardError` with a message safe to show the model/user on any violation.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise SQLGuardError(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise SQLGuardError("Exactly one SQL statement is allowed per call")
    stmt = statements[0]

    if not isinstance(stmt, exp.Select):
        raise SQLGuardError("Only SELECT statements (optionally with a WITH clause) are allowed")
    if stmt.args.get("into") is not None:
        raise SQLGuardError("SELECT INTO is not allowed — it creates a table")

    for cte in stmt.find_all(exp.CTE):
        if not isinstance(cte.this, exp.Select):
            raise SQLGuardError("Every WITH clause must wrap a SELECT, not an INSERT/UPDATE/DELETE")

    for func in stmt.find_all(exp.Anonymous):
        name = str(func.this).lower() if func.this else ""
        if name in _DENIED_FUNCTIONS:
            raise SQLGuardError(f"Function {name!r} is not allowed")

    allowed = {s.lower() for s in allowed_schemas}
    for table in stmt.find_all(exp.Table):
        schema = (table.db or "public").lower()
        if schema not in allowed:
            raise SQLGuardError(
                f"Schema {schema!r} is not in this connector's allowed schema list "
                f"{sorted(allowed)}"
            )

    return _clamp_limit(stmt, row_limit).sql(dialect="postgres")


def _clamp_limit(stmt: exp.Select, row_limit: int) -> exp.Select:
    existing = stmt.args.get("limit")
    if existing is not None:
        try:
            if int(existing.expression.this) <= row_limit:
                return stmt
        except (AttributeError, TypeError, ValueError):
            pass  # non-literal or unparseable LIMIT — fall through and clamp it
    return stmt.limit(row_limit)
