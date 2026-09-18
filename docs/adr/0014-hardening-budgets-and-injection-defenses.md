# ADR-0014: Hardening — budgets, limits, injection defenses and evals

## Status

Accepted. Records the decisions Phase 8 (docs/system-design.md section 28) was built on. Leaves
[ADR-0008](0008-cut-observability-stack.md) in force and extends
[ADR-0009](0009-phase3-connector-metadata-in-code.md)'s deferral of the `eval_*` tables.

## Context

Phase 8's "done when" is that every CI gate is green and there is an experiments table with real
numbers. Section 28 also lists Prometheus, Grafana, alerts and Sentry; ADR-0008 cut all four, so
the rest of the phase — audit, usage, budgets, rate and concurrency limits, the watchdog and
retention, injection defenses, log scrubbing, PII redaction, and the missing eval suites and
harness features — is the whole bar.

Most of these had a shape already waiting for them: `Budget` on `AgentState` carried all five
limits, `workspace_policies` held every setting at its design default, `structlog` was a
dependency nobody called. The decisions below are where the obvious build was not the one taken.

## Decision

### 1. The observability stack stays cut

No `/metrics`, because nothing would scrape it. What section 20.2's metric table wanted to show
(cost by model and node, tool reliability by connector) is `GET /workspaces/{ws}/usage`: one SQL
aggregate over `llm_calls` and one over `tool_calls`. `structlog` is configured once
(`relay_core/observability/logging.py`) for JSON lines carrying `request_id` and `run_id`, which
is the half of section 20.1 that survives without a tracing backend.

*Rejected:* a `/metrics` endpoint "for later". An endpoint with no scraper is an unauthenticated
surface with no reader.

### 2. A run budget stop is an answer; a monthly budget stop is a 402

When a run spends its budget, `execute_step` marks the step failed with the reason, `next_step`
skips the rest, and `synthesize` is told to say the answer is partial and what was covered. LLM
spend is read back from `llm_calls` before each check rather than reported by every node, so no
node can forget to count itself. The wall clock resets when `approval_gate` resumes, so time
parked waiting for a human is not the run's spend.

The monthly budget refuses at enqueue with an RFC 9457 402 and writes no run row, so section
14.3's `budget_exceeded` status is not added — no row could ever hold it.

### 3. Rate limits reuse the fixed-window limiter

Section 15.6 says sliding window. `relay_core.llm.ratelimit` already had a fixed-window counter in
production, so it was generalised into a keyed `hit()` that both the LLM limiter and the HTTP
limits call. Limits are settings, because a shared Redis window tipped the Phase 7 test suite
into `RateLimitExceeded`, and a Redis outage lets requests through.

*Rejected:* a second, sliding-window algorithm for a smoother curve nobody would measure.

### 4. The conversation lock is the runs table, not a Redis key

Section 19.3 suggests `SET NX` on `conv:{id}:lock`. A run row's status already says whether a run
is active, and it cannot outlive its run, whereas a Redis lock needs a TTL and a release path that
a killed worker skips. The concurrency permit *is* in Redis — a sorted set scored by expiry, so a
dead worker's permit lapses on its own — because that is state no row holds.

A run parked at `awaiting_approval` keeps the lock (the conversation is busy) but has released
its permit, because the worker exits at `interrupt()`.

### 5. Injection is detected and surfaced, not silently dropped

A cheap pattern prefilter decides which tool results are worth one `LIGHT` classifier call, and
errs toward calling. A `suspicious` verdict leaves the content in place, still wrapped, with the
detection named in the tag; it is recorded on `tool_calls.output`, published as
`content.flagged`, and audited. A classifier failure means no flag, never a failed run.

*Rejected:* dropping flagged content. A legitimate email that says "please forward this to
finance" would vanish from the answer with no explanation, and the user would conclude the agent
cannot read email.

### 6. The data-flow rule is one flag, not a lineage graph

Once a run has read from a connector marked `untrusted_source` (the default for anything that
reaches outside Relay), or has had any content flagged, every later write needs approval —
through a `never` override and for an owner. The approval summary names the source, so an
approver knows why a normally silent action is asking.

*Rejected:* per-field data lineage (which bytes reached which argument). That is a research
problem; "this run touched the web" is a boolean and covers section 18.1's exfiltration row.

### 7. PII is redacted for the model and restored at the edges

With `workspace_policies.pii_redaction` on (the default), email addresses and phone numbers in
tool output become stable placeholders before the classifier or the model sees them. The mapping
lives on run state; `ToolExecutor` restores real values in a tool's arguments before they reach
the connector — so the email allow-list inside the connector still checks the real domain — and
`validate_final`/`finalize` restore them in the answer the user reads.

### 8. `eval_*` tables stay deferred

The JSON reports under `evals/reports/` plus `relay-eval compare` are what the experiments table
is built from. The tables would feed an Evals page, and that page is frontend work not happening
this phase.

*Rejected:* building the tables now. A table with no reader is a migration with no payoff.

### 9. The fabrication judge ships with its gate off

The judge (`relay_eval/judge.py`, a pinned `MODEL_EVAL_JUDGE`) runs on every
`fabrication_check` case and reports what it finds, but only fails cases under
`--fabrication-gate`. Section 21.4 asks for ~50 hand labels and an agreement figure first, and no
real run exists to label yet. A red gate nobody trusts gets disabled within a week.

## Consequences

- Every experiment toggle is a setting defaulting to production behaviour (evals/EXPERIMENTS.md),
  so the experiments exercise the production path.
- The conversation lock has a narrow race (two messages in the same instant both pass the check),
  noted in code with the partial unique index that would close it.
- `config_matrix` gates each profile against its own recorded baseline, not against `full`, and
  records one on its first run.
- The experiments table and the fabrication calibration both wait on a funded real-Gemini run.
  Until then Phase 8's "done when" is met in code and unmet in evidence.
