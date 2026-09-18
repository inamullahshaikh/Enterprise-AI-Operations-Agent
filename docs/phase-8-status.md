# Phase 8: Hardening (status)

**Last updated:** 18 September 2026
**Design doc:** [system-design.md](system-design.md) §14.3, §14.5, §15.5–15.6, §18.1, §18.4, §19.1–19.3, §20.4, §21.1, §21.4, §21.6, §28 Phase 8
**Tickets:** [phase-8-tickets.md](phase-8-tickets.md)
**Decisions:** [adr/0014](adr/0014-hardening-budgets-and-injection-defenses.md)
**Working tree:** all of Phase 8 is **uncommitted** on top of `6452db9`.

All 16 tickets are built. **Done when (§28):** every CI gate green, and an experiments table with
real numbers. The first half holds for everything that runs without Gemini. The second half does
not hold yet: no eval suite has been run against real Gemini — not the new ones, and not the
Phase 3–7 suites either — so [EXPERIMENTS.md](../evals/EXPERIMENTS.md)'s table is empty. See
"Not yet verified".

---

## Done: 16 of 16 tickets

### Workstream A: audit and usage

| Ticket | Summary |
| ------ | ------- |
| **A1** | `audit_logs` (the phase's only migration): `bigserial` id, index `(workspace_id, created_at DESC, id DESC)`. `AuditLogRepository.record` scrubs `details` itself; `list_for_workspace` pages newest-first on `(created_at, id)`. `GET /workspaces/{ws}/audit-logs` (admin) with `action`/`actor_user_id`/`before`/`limit`. Rows written for connector install/update/delete, credentials written, OAuth connected, approval decided, member added/role changed/removed, tool updated, memory deleted, policy updated, monthly budget changed, content flagged, approval forced by the data-flow rule, and retention runs. `ip` comes from a request middleware. |
| **A2** | `GET /workspaces/{ws}/usage?from=&to=&group_by=day\|model\|node` (admin): one aggregate over `llm_calls`, one over `tool_calls` by connector and status. Defaults to the current UTC month (`month_window`, shared with B2). |

### Workstream B: budgets, limits, durability

| Ticket | Summary |
| ------ | ------- |
| **B1** | `load_context` loads `Budget` from `workspace_policies.run_budget`. `enforce_budget` checks tool calls, LLM calls, cost and wall time; LLM spend is re-read from `llm_calls` each iteration. A spent budget fails the step, skips the rest, publishes `budget.exceeded` once, and `synthesize` says the answer is partial. `approval_gate` resets the wall clock on resume. |
| **B2** | The message route refuses with an RFC 9457 402 (limit and reset date) when this month's spend ≥ `monthly_budget_usd`, before any run row. Spend cached 60 s in Redis under `budget:{ws}:{yyyy-mm}`, dropped by `finalize` and by `PATCH /workspaces/{ws}` (owner only, audited). |
| **B3** | `relay_core.llm.ratelimit.hit()` is the keyed fixed-window counter both limiters use. `relay_api/ratelimit.py` guards the message route (per user and per workspace) and the connector test route. `RateLimit-*` headers on every guarded response, 429 + `Retry-After` when over. Redis down lets traffic through. |
| **B4** | Worker-side permits in a Redis sorted set scored by expiry (`relay_core/agent/concurrency.py`); over the limit the Celery task retries with backoff and the run stays `queued`. The conversation lock is a read of the runs table: a second message while a run is `queued`/`running`/`awaiting_approval` gets 409 naming it. |
| **B5** | `relay_core/maintenance.py`: `fail_stalled_runs` (every 5 min; no `llm_calls` or `tool_calls` activity for 10 min → `failed`, code `stalled`) and `apply_retention` (nightly; clears `tool_calls.output`, deletes attachment blobs and rows, deletes LangGraph threads of idle conversations; per-workspace savepoint and commit; one audit row each; `data_retention_days = 0` skipped). |

### Workstream C: injection defenses

| Ticket | Summary |
| ------ | ------- |
| **C1** | `relay_core/security/injection.py`: `looks_like_instructions` prefilter and a `LIGHT` `classify`. Run on each successful tool result in `execute_step`. A suspicious result stays in the model's context, wrapped, with `injection_suspected="<technique>"` on the tag, and is recorded on `tool_calls.output`, published as `content.flagged` and audited. |
| **C2** | `Connector.untrusted_source` (default True; False for postgres, file_upload, documents, python_sandbox). A read from an untrusted source, or any flagged content, sets `touched_untrusted`; `needs_approval` then requires approval for every write regardless of overrides or role. The approval summary names the source; an audit row records the forced approval. |
| **C3** | `relay_core/security/scrub.py` (by key and by credential shape, sharing the memory extractor's patterns) behind `structlog` JSON logging with `request_id` (API middleware, echoed as `X-Request-ID`) and `run_id` (runner). `relay_core/security/pii.py`: emails and phones become `<email:N>`/`<phone:N>` before the model sees tool output; restored in tool arguments by `ToolExecutor` and in the answer by `validate_final`/`finalize`. |

### Workstream D: evals and experiments

| Ticket | Summary |
| ------ | ------- |
| **D1** | `evals/suites/injection/` (5 cases: MCP ticket, web page, email, CSV cell, KB footer; gate 0.98). Payloads in `evals/fixtures/injection_payloads.yaml`, seeded through new `/_inject` routes on the mock service and the ticketing server. Scored on what ran: `must_not_call_tools` and `must_not_target` (the attacker's address in any executed call's arguments). |
| **D2** | `evals/judges/fabrication.md` and `relay_eval/judge.py` (pinned `MODEL_EVAL_JUDGE`). `fabrication_check` on all four `capability_detection` cases. A judge failure is `unscored`, not a failure. `relay-eval calibrate` reports Cohen's kappa against `fabrication_labels.yaml`. **The gate is off** (`--fabrication-gate` not passed in CI): the labels file is empty because no real run exists to label. |
| **D3** | `evals/suites/config_matrix/` (10 cases over 4 tasks across `full`/`db_only`/`csv_only`/`docs_only`/`none`). Gated per profile against `baseline.yaml`, 5-point tolerance; the first run records the baseline and passes. `--update-baseline` re-records it deliberately. |
| **D4** | `--repeats` and `--concurrency` work. Reports gain `cases` (attempts, pass@1, pass^n), per-profile pass rates and total cost; `compare` shows pass@1 and cost. Concurrency drops to 1 for any suite with a `depends_on_case` edge **or a `full`-profile case**. Nightly run added to `ci.yml`. |
| **D5** | Six toggles, all settings defaulting to production behaviour, each with a test that the non-default changes behaviour: `EXPERIMENT_SINGLE_REACT`, `TOOL_RETRIEVAL_ENABLED`, `MODEL_PLANNER_THINKING` (existing), `RAG_HYBRID`/`RAG_RERANK`/`RAG_EMBED_CONTEXT_HEADERS`, `WRAP_UNTRUSTED_OUTPUT`, and the postgres `schema_annotations` config (`EXPERIMENT_SQL_SCHEMA_ANNOTATIONS` in the harness). `MCP_TICKETING_EXTRA_TOOLS=N` gives experiment 2 its 60+ tools. [evals/EXPERIMENTS.md](../evals/EXPERIMENTS.md) holds the method and commands. **No experiment has been run.** |

### Workstream E: docs

| Ticket | Summary |
| ------ | ------- |
| **E1** | [adr/0014](adr/0014-hardening-budgets-and-injection-defenses.md), this note, the README Phase 8 paragraph and experiments table, `.env.example`. |

**Tests:** 178 unit, 255 integration. `ruff` and `mypy` are clean on `relay_core`, `relay_api`,
`relay_worker`, `tests`, `alembic`.

```bash
cd backend
python -m pytest tests/unit -q
python -m pytest tests/integration -q            # needs a running Docker daemon
python -m ruff check relay_core relay_api relay_worker tests alembic
python -m mypy relay_core relay_api relay_worker
```

`tests/conftest.py` now puts `evals/relay_eval` on `sys.path`, so the eval harness no longer has
to be installed for either suite to collect.

Checks confirmed to go red against the thing they guard:

- `test_eval_injection_gate.py` scripts a model that obeys the payload. With the classifier
  returning clean and the source trusted, the draft to the attacker runs and `injection_failures`
  reports it; with the data-flow rule left on, the write is only proposed and the case is clean.
- `test_injection_and_data_flow.py::test_ordinary_mail_costs_no_classifier_call` fails if the
  prefilter regresses into calling the classifier on everything (the scripted model runs out of
  responses).
- `test_run_budget.py` asserts `budget.exceeded` is published exactly once and that a budget
  under one call's cost makes no executor call at all.

---

## Not yet verified

- **Any real-Gemini eval run, and therefore the "done when".** The experiments table and the
  judge calibration both need a funded run with `GEMINI_API_KEY`, the demo database, the mock
  service and the ticketing server. The phase-8 tickets said to run `relay-eval run --all` once
  as a baseline before Workstream C; that was not possible here either.
- **The `injection` suite against a real model.** Five cases are written to tempt; whether a real
  model takes the bait (so the defenses are what keep the gate green) is unknown until run.
- **`config_matrix` expectations.** The per-profile expectations (which capability each
  profile reports missing) are the ones `capability_detection` already uses, but the tasks have
  not been run in every profile.
- **The Celery side of B4 and B5.** Permit acquire/release around `run_agent`/`resume_agent`,
  task retry with backoff, and the two new beat entries are not exercised by any test that runs a
  worker; the logic under them is tested directly.
- **`apply_retention` against real R2 and a real `AsyncPostgresSaver`.** Tested with fakes for
  the blob store and checkpointer.
- **The nightly workflow.** `ci.yml`'s schedule trigger and artifact upload have not run on
  GitHub.

---

## Decisions made that aren't in the design doc

The main ones are in [adr/0014](adr/0014-hardening-budgets-and-injection-defenses.md). Smaller:

1. **`GET`/`PATCH /workspaces/{ws}/policy` (admin) were added** so a policy change had a route to
   audit. `approval_rules` is validated strictly on write (422), unlike the lenient runtime read;
   `run_budget` merges into the stored one.
2. **The problem-details handler now forwards exception headers.** It was dropping them, which
   also meant `WWW-Authenticate` on 401s never reached clients. B3's `Retry-After` found it.
3. **The audit client IP comes from a context variable** set by one middleware, so no route
   threads `Request` through. It is the direct peer address; behind a proxy it will be the proxy's.
4. **`budget_exhausted` and `touched_untrusted` are strings, not booleans,** so the synthesis
   prompt and the approval card can say *why*.
5. **The injection classifier sees PII-redacted text.** Redaction runs after the real output is
   written to `tool_calls` and before either the classifier or the executor model reads it.
6. **The memory extractor's credential regex was split** into value shapes (shared with the
   scrubber) and keywords (memory only), so log lines mentioning "password" are not mangled.
7. **Concurrency is capped at 1 for suites with `full`-profile cases**, not only for
   `depends_on_case` suites: `full` cases reset and count the shared mock services.
8. **The nightly eval run is a scheduled trigger on `ci.yml`**, not a separate workflow file, so
   the job's services and setup exist once.

---

## Known rough edges

- **The conversation lock can race**: two messages in the same instant both pass the check. A
  partial unique index on active runs per conversation would close it.
- **Permit acquisition is check-then-add**, so two workers can both take the last permit.
- **Placeholders show in approval cards.** The model proposes `to: <email:1>`; the approver sees
  that, and `ToolExecutor` resolves it at execution. The card should resolve it too.
- **`approval_gate`'s held reads are not redacted or screened** — only `execute_step`'s results
  are. They are rare (reads alongside a gated write in one model turn).
- **The phone pattern redacts any 9–15 digit run in a string**, including long ids. They are
  restored before any tool call, so a query still works, but the model loses the value.
- **Retention deletes checkpoints by conversation idle time**, the only per-thread timestamp
  available; a conversation touched yesterday keeps a months-old checkpoint.
- **No frontend.** The run inspector, usage and audit-log pages, and the 402/409/partial-answer
  states in chat are deferred to the frontend pass; the routes above are the contract.
