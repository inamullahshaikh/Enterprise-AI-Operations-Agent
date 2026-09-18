# Experiments (docs/system-design.md §21.6)

Six comparisons, each a pair of `relay-eval run` passes with exactly one setting changed. Every
toggle is a setting whose default is production behaviour, so an experiment arm runs the
production code path with one switch flipped — never a fork of it. Each toggle has an integration
test proving the non-default value actually changes behaviour (listed per experiment), because a
toggle that silently does nothing produces a beautifully clean result.

> **Status: not yet run.** The harness, suites and toggles are in place, but no arm has been run
> against real Gemini. The results table below is empty on purpose, and so is the one in the
> README. Filling it needs `GEMINI_API_KEY`, the demo database, the mock services and the sample
> MCP server (see [README.md](README.md)). Record each run's date, command, report filenames and
> cost in the table when it happens.

## Method

- One repeat per arm (`--repeats 1`) unless the two arms land within 5 points of each other; then
  re-run both arms with `--repeats 3` and report pass@1 and pass^3 from the report's `cases`
  block. No significance testing: "we ran it three times and it went the same way" is the claim
  available at this sample size.
- Compare arms with `relay-eval compare <baseline.json> <variant.json>`. It prints per-case
  outcomes, pass rate and total cost for both reports.
- Report the cost of both arms, from each report's `cost_usd`. Six paired `--all`-sized runs are
  the largest Gemini bill this project runs up; what it cost is part of the result.
- The eval workspaces (`eval-db-only`, `eval-docs-only`, `eval-full`, ...) persist between runs.
  An experiment whose toggle is read **at install or ingest time** (4c, 6) needs those workspaces
  dropped first so they are recreated under the new setting:

  ```sql
  DELETE FROM workspaces WHERE slug LIKE 'eval-%';
  ```

  Run it against Relay's own database before *both* arms, so the baseline is not advantaged by a
  warm workspace.

## The experiments

All commands run from `backend/` with the eval harness installed. Settings are environment
variables (`relay_core/config.py`).

### 1. Single ReAct loop vs plan-and-execute

`EXPERIMENT_SINGLE_REACT=true` skips the planner and hands `execute_step` one step whose goal is
the whole objective, with every available capability optional. That *is* a single ReAct loop, on
the same executor, tools and validators. Test: `test_single_react_experiment_skips_the_planner`.

```bash
relay-eval run --all
EXPERIMENT_SINGLE_REACT=true relay-eval run --all
```

Report: pass rate, cost, latency (`latency_s` per result).

### 2. Tool retrieval on vs off with 60+ tools

Start the sample MCP server with filler tools so the `full` workspace has 60+ tools, then compare
with retrieval on and off. Test: `test_with_retrieval_off_every_tool_binds`.

```bash
MCP_TICKETING_EXTRA_TOOLS=60 uvicorn server:app --port 8200   # in mcp_examples/ticketing
relay-eval run --suite tool_selection
TOOL_RETRIEVAL_ENABLED=false relay-eval run --suite tool_selection
```

Report: tool-selection pass rate and input tokens (`GET /workspaces/{eval-full}/usage?group_by=node`
over the run window, or `llm_calls.input_tokens` summed per run).

### 3. Planner thinking level: low vs high

```bash
MODEL_PLANNER_THINKING=high relay-eval run --all
MODEL_PLANNER_THINKING=low relay-eval run --all
```

Already a setting since Phase 1; no new test.

### 4. Retrieval: hybrid vs vector-only, contextual headers, reranking

Three pairs over the `rag` suite. Tests: `test_rerank_and_keyword_leg_can_be_switched_off`,
`test_contextual_headers_can_be_embedded_with_each_chunk`.

```bash
relay-eval run --suite rag                                  # baseline for 4a and 4b
RAG_HYBRID=false relay-eval run --suite rag                 # 4a: vector-only
RAG_RERANK=false relay-eval run --suite rag                 # 4b: no rerank
# 4c: drop the eval workspaces before each arm (see Method): the header is embedded at ingest
RAG_EMBED_CONTEXT_HEADERS=false relay-eval run --suite rag
RAG_EMBED_CONTEXT_HEADERS=true relay-eval run --suite rag
```

### 5. Untrusted-content wrapping on vs off

Against the `injection` suite. This is the experiment most likely to produce an uncomfortable
number, and publishing it is the point. Test: `test_unwrapped_experiment_drops_the_untrusted_tag`.

```bash
relay-eval run --suite injection
WRAP_UNTRUSTED_OUTPUT=false relay-eval run --suite injection
```

Both arms keep the injection classifier and the data-flow rule on, so this measures the wrapping's
own contribution on top of them. To see the model's unaided resistance, the second arm can also
be run with the classifier and rule disabled in code; that is not a supported setting.

### 6. Schema annotations on vs off

`describe_table` with and without primary and foreign keys, over `text_to_sql`. The value is read
when the eval workspace's postgres installation is created, so drop the eval workspaces before
each arm. Test: `test_describe_table_without_schema_annotations_omits_the_keys`.

```bash
EXPERIMENT_SQL_SCHEMA_ANNOTATIONS=true relay-eval run --suite text_to_sql
EXPERIMENT_SQL_SCHEMA_ANNOTATIONS=false relay-eval run --suite text_to_sql
```

## Results

| # | Experiment | Suite | Baseline | Variant | Δ | Cost (both arms) | Date | Reports |
|---|------------|-------|----------|---------|---|------------------|------|---------|
| 1 | Plan-and-execute vs single ReAct | all | — | — | — | — | not run | — |
| 2 | Tool retrieval on vs off (64 tools) | tool_selection | — | — | — | — | not run | — |
| 3 | Planner thinking high vs low | all | — | — | — | — | not run | — |
| 4a | Hybrid vs vector-only | rag | — | — | — | — | not run | — |
| 4b | Rerank on vs off | rag | — | — | — | — | not run | — |
| 4c | Contextual headers off vs on | rag | — | — | — | — | not run | — |
| 5 | Untrusted wrapping on vs off | injection | — | — | — | — | not run | — |
| 6 | Schema annotations on vs off | text_to_sql | — | — | — | — | not run | — |
