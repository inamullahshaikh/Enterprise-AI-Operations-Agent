"""Relay eval harness (docs/system-design.md section 21). Runs the `routing`,
`capability_detection`, and `text_to_sql` suites through the real agent graph and
repositories — see `relay_eval.harness` for how, and docs/adr/0009 for what's deliberately
deferred to later phases (mocked-connector suites, LLM-as-judge scoring, DB-persisted runs)."""
