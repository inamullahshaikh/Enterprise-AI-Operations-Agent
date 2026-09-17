# Relay eval harness

Ten suites are planned (routing, planning, capability_detection, tool_selection, text_to_sql,
rag, task_success, approval_compliance, injection, config_matrix). Eight exist so far:
`routing`, `capability_detection`, `text_to_sql` and `rag` (Phases 3–4),
`approval_compliance` and `task_success` (Phase 5), `tool_selection` (Phase 6) and `planning`
(Phase 7). See docs/system-design.md section 21 for the
full design and docs/adr/0009-phase3-connector-metadata-in-code.md for what v0 deliberately
trims: no `eval_*` DB tables (a JSON report under `reports/` instead), no LLM-as-judge scoring
(deterministic checks only — the `rag` suite's "recall@k/faithfulness/citation accuracy" row
from section 21.1 is scored today by substring `must_mention`/`must_not_mention` checks against
the final answer and `must_call_tools` glob checks against successful tool calls, not a judge;
see `relay_eval.scoring`).

**Cases can depend on each other.** `depends_on_case: <key>` runs a case after the named one, in
the same workspace. `task_success`'s memory pair uses it: the first run states a preference, the
second never mentions it, and only memory retrieval can put it back in front of the model.
Ordering is enough because a profile's cases already share one workspace and memory is scoped to
the workspace and user, not the conversation. The harness runs memory extraction inline, since
nothing is consuming the `memory` queue behind it. Note that the `eval-full` workspace persists
across harness runs, so a memory from an earlier run survives — re-running the suite does not
re-prove extraction.

**Write cases park and resume.** When a run stops for approval, the harness decides through the
real decision route function according to the case's `approval_decision` (`approve_all`,
`approve_first`, `reject_all`), resumes the run, and repeats until the run stops asking. Every
case in every suite is scored for **approval violations**: a write that reached its connector
without the script approving it, or more writes seen by the mock service than were recorded. Any
violation fails `--ci`, and `approval_compliance` also needs a 100% pass rate, so a model that
never attempts its write can't pass it by default. See
docs/adr/0011-approvals-interrupt-resume-and-enforcement-in-code.md.

## Install (once, alongside the backend)

```bash
cd backend && pip install -e ".[dev]"
cd ../evals/relay_eval && pip install -e .
```

Needs a real `GEMINI_API_KEY` and a reachable Postgres/Redis (the same ones the backend uses) —
this runs through the real graph and repositories, not a scripted fake client. The `full`
profile (postgres + mock gmail/google_calendar) also needs the mock service at
`MOCK_SERVICES_URL` (`docker compose up mock-services`, then `MOCK_SERVICES_URL=http://localhost:8100`).
The `full` profile also installs `web_search` against the mock service and an `mcp` connector
against the sample ticketing server at `MCP_TICKETING_URL` (`docker compose up mcp-ticketing`,
then `MCP_TICKETING_URL=http://localhost:8200/mcp`). Installing it runs the real capability
tagger. Outside Docker, both services are on `localhost`, so also set
`SSRF_ALLOWED_HOSTS='["localhost"]'`. The harness resets both services before each `full` case,
wiping any demo drafts or tickets in them. A case message can say `{mock_services_url}` for a page
on the mock service.

```bash
relay-eval run --suite text_to_sql
relay-eval run --all --ci            # exits non-zero if any suite misses its gate
relay-eval compare reports/text_to_sql_20260101T000000Z.json reports/text_to_sql_20260102T000000Z.json
```

`--profile`/`--repeats`/`--concurrency` are accepted for compatibility with the CLI shown in
section 21.5 but aren't implemented yet — each case declares its own `connector_profile`, and
every run is a single sequential pass over each suite's cases.

- `suites/<suite>/*.yaml` — eval cases (see section 21.3 for the format; the subset built so
  far is in `relay_eval.cases.EvalCase`)
- `suites/approval_compliance/`, `suites/task_success/` — write cases on the `full` profile
- `suites/tool_selection/` — the agent picks discovered tools: MCP `search_tickets`, web
  `search_web` and `fetch_url` (gate 0.9)
- `suites/planning/` — the first plan cannot work and the run has to revise it (gate 0.9). Scored
  by `expectations.expect_replan`, which counts `llm_calls` rows with `node = 'planner'`: `plan`
  and `replan` share that role, so two calls in one run means the plan changed. Both cases must
  pass — a replan that only handles one of "the tool failed" and "the result made the next step
  pointless" is half a feature.
- `fixtures/` — CSV fixtures for the `csv_only` connector profile and Markdown fixtures for
  `docs_only` (ingested into that profile's eval workspace once, idempotently, by
  `relay_eval.workspace_setup._ensure_documents_ingested` — same two documents as
  `demo/documents/`, copied rather than shared so the harness doesn't depend on `demo/` being
  present). Gmail/Calendar need no recorded fixtures: `mocks/main.py` seeds its own inbox and
  calendar from the same demo accounts.
- `judges/` — LLM-as-judge rubrics (Phase 8)
- `reports/` — JSON reports written by `relay-eval run` (gitignored except `.gitkeep`)
- `relay_eval/` — the harness CLI, its own installable package
