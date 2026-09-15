# Relay eval harness

Ten suites (routing, planning, capability_detection, tool_selection, text_to_sql, rag,
task_success, approval_compliance, injection, config_matrix) run against mocked
connectors for reproducible, free results, plus a smaller nightly suite against real
sandboxes. See docs/system-design.md section 21 for the full design, case format, and CI gates.

```bash
relay-eval run --suite task_success --profile full --repeats 3 --concurrency 4
relay-eval run --all --ci
relay-eval compare <run_a> <run_b>
```

- `suites/<suite>/*.yaml` — eval cases (see section 21.3 for the format)
- `fixtures/` — recorded API responses used by the mocked connectors
- `judges/` — LLM-as-judge rubrics
- `relay_eval/` — the harness CLI (its own installable package, added in Phase 3)

Started in Phase 3 (routing, capability_detection, text_to_sql), extended suite by suite
through Phase 8.
