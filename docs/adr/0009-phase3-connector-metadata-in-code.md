# ADR-0009: Phase 3 connector/tool metadata lives in code, not new DB tables

## Status

Accepted

## Context

docs/system-design.md section 14.3 defines `connector_definitions`, `tool_definitions`,
`capability_bindings`, and `run_steps` tables. Phase 3 (section 28) only needs two built-in
connectors (`postgres`, `file_upload`), neither of which has dynamic tool discovery, admin
tool review, or admin-configurable capability priorities — those features belong to MCP/OpenAPI
(section 6.4-6.5) and the "capabilities page with priorities" UI, both explicitly Phase 6 work.
Building the full four-table schema now would add migrations, repositories, and CRUD surface
that nothing in Phase 3 reads or writes, which cuts against how Phases 1-2 were built (see e.g.
`relay_core/agent/state.py`'s own docstring on not adding fields "nothing reads yet").

## Decision

For Phase 3:

- **No `connector_definitions` table.** Each built-in connector ships a YAML manifest
  (`relay_core/connectors/manifests/*.yaml`: display name, category, description, auth type,
  config/secrets JSON Schema, `provides_capabilities`) loaded into an in-memory registry at
  import time (`relay_core/connectors/manifest.py`). `GET /connectors/catalog` reads this
  registry, not a table.
- **No `tool_definitions` table.** `ToolRegistry.tools_for_run` calls `connector.list_tools(ctx)`
  live on every run instead of reading a cache populated by a discovery job. This is fine for
  two built-ins with a handful of tools each; it stops being fine once MCP/OpenAPI tool counts
  and discovery scheduling (section 6.4) show up in Phase 6, which is where this table gets built.
- **No `capability_bindings` table.** `connector_installations` gets a `priority` column
  (smallint, default 100, lower preferred) directly. The capability resolver
  (`relay_core/capabilities/resolver.py`) queries active/healthy installations whose manifest
  declares a capability, ordered by that column. There is no admin UI to edit priorities yet
  (Phase 6), so a separate bindings table would only ever hold one row per
  (workspace, capability, installation) anyway.
- **No `run_steps` table.** `tool_calls` (added this phase) stores the plan step key
  (`plan_step_id text`, e.g. `"s1"`) directly instead of a foreign key to a `run_steps` row.
  The per-step status/attempts/result_summary state that `run_steps` would otherwise hold
  already lives in `agent_runs.plan` (the `Plan`/`PlanStep` JSON, checkpointed by LangGraph)
  for the duration of the run. A durable, queryable `run_steps` table earns its keep once
  something reads it independently of a live run — the Phase 8 run-inspector page.
- **Eval harness v0 has no `eval_suites`/`eval_cases`/`eval_runs`/`eval_results` tables.**
  `evals/relay_eval` loads cases straight from the suite YAML files and writes a JSON report to
  `evals/reports/`. DB persistence, trend charts, and an Evals page are Phase 8 polish once the
  harness has years^H^H^Hmultiple suites' worth of history worth querying.

## Consequences

- Migrating a built-in connector's manifest requires a code change + redeploy, not an admin UI
  edit — acceptable while the only editors are the two people building this.
- The capability resolver is coarser than the full design: a connector either provides a
  capability (per its manifest) everywhere it's installed and healthy, or it doesn't. There is
  no per-table "this schema's `subscriptions` table maps to `subscription.read`" annotation
  (section 10.1's "semantic mapping") — deferred alongside admin-configurable priorities.
- `tool_calls.plan_step_id` is a plain string, not an FK — a query joining tool calls back to
  the step that produced them has to match on `(run_id, plan_step_id)` against the JSON plan,
  not a foreign key. Acceptable for the eval harness and no UI yet reads this join.
- Every one of these is additive later: new tables slot in without touching the Phase 3 shape,
  the same way `capability_bindings` slotting in later doesn't change what
  `check_capabilities` already does today.

## Alternatives considered

- Build the full section 14.3 schema now, populate `tool_definitions` from a one-time sync at
  install time even though nothing refreshes it on a schedule yet: rejected — that's a table
  with fake staleness semantics (it claims to be a live cache but isn't kept live) for no
  present benefit.
