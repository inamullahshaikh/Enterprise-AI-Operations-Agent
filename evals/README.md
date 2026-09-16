# Relay eval harness

Ten suites are planned (routing, planning, capability_detection, tool_selection, text_to_sql,
rag, task_success, approval_compliance, injection, config_matrix); three exist so far —
`routing`, `capability_detection`, `text_to_sql` — built against the two Phase 3 connectors
(`postgres`, `file_upload`). See docs/system-design.md section 21 for the full design and
docs/adr/0009-phase3-connector-metadata-in-code.md for what v0 deliberately trims: no mocked
connectors yet (nothing to mock until Gmail/Calendar/HubSpot exist), no `eval_*` DB tables (a
JSON report under `reports/` instead), no LLM-as-judge scoring (deterministic checks only).

## Install (once, alongside the backend)

```bash
cd backend && pip install -e ".[dev]"
cd ../evals/relay_eval && pip install -e .
```

Needs a real `GEMINI_API_KEY` and a reachable Postgres/Redis (the same ones the backend uses) —
this runs through the real graph and repositories, not a scripted fake client.

```bash
relay-eval run --suite text_to_sql
relay-eval run --all --ci            # exits non-zero if any suite misses its gate
relay-eval compare reports/text_to_sql_20260101T000000Z.json reports/text_to_sql_20260102T000000Z.json
```

`--profile`/`--repeats`/`--concurrency` are accepted for compatibility with the CLI shown in
section 21.5 but aren't implemented yet — each case declares its own `connector_profile`, and
every run is a single sequential pass over each suite's cases.

- `suites/<suite>/*.yaml` — eval cases (see section 21.3 for the format; the Phase 3 subset is
  in `relay_eval.cases.EvalCase`)
- `fixtures/` — CSV fixtures for the `csv_only` connector profile (recorded API responses for
  mocked connectors land here once Phase 5+ builds suites that need them)
- `judges/` — LLM-as-judge rubrics (Phase 8)
- `reports/` — JSON reports written by `relay-eval run` (gitignored except `.gitkeep`)
- `relay_eval/` — the harness CLI, its own installable package
