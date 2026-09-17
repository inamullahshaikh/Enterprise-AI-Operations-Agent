# Relay eval harness

Ten suites are planned (routing, planning, capability_detection, tool_selection, text_to_sql,
rag, task_success, approval_compliance, injection, config_matrix). Six exist so far:
`routing`, `capability_detection`, `text_to_sql` and `rag` (Phases 3–4), plus
`approval_compliance` and `task_success` (Phase 5). See docs/system-design.md section 21 for the
full design and docs/adr/0009-phase3-connector-metadata-in-code.md for what v0 deliberately
trims: no `eval_*` DB tables (a JSON report under `reports/` instead), no LLM-as-judge scoring
(deterministic checks only — the `rag` suite's "recall@k/faithfulness/citation accuracy" row
from section 21.1 is scored today by substring `must_mention`/`must_not_mention` checks against
the final answer and `must_call_tools` glob checks against successful tool calls, not a judge;
see `relay_eval.scoring`).

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
The harness resets that service before each `full` case, wiping any demo drafts in it.

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
- `fixtures/` — CSV fixtures for the `csv_only` connector profile and Markdown fixtures for
  `docs_only` (ingested into that profile's eval workspace once, idempotently, by
  `relay_eval.workspace_setup._ensure_documents_ingested` — same two documents as
  `demo/documents/`, copied rather than shared so the harness doesn't depend on `demo/` being
  present). Gmail/Calendar need no recorded fixtures: `mocks/main.py` seeds its own inbox and
  calendar from the same demo accounts.
- `judges/` — LLM-as-judge rubrics (Phase 8)
- `reports/` — JSON reports written by `relay-eval run` (gitignored except `.gitkeep`)
- `relay_eval/` — the harness CLI, its own installable package
