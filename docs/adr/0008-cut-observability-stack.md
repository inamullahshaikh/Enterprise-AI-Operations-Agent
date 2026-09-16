# ADR-0008: Cut the external observability stack (Langfuse, OpenTelemetry, Sentry)

## Status

Accepted

## Context

docs/system-design.md section 20 specifies a full observability stack: Langfuse for
LLM-specific tracing, OpenTelemetry + a Tempo/Jaeger-style backend for distributed
request tracing, Prometheus + Grafana for metrics and dashboards, and Sentry for
error/crash tracking. Setting this up means creating and maintaining several external
accounts/services (Langfuse, Sentry) and running a collector + tracing backend
(OpenTelemetry), for a portfolio-scale project that doesn't need production-grade
observability to be demoed or evaluated.

## Decision

Cut Langfuse, OpenTelemetry, and Sentry from scope entirely. `LANGFUSE_HOST`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and
`SENTRY_DSN` have been removed from `.env.example` and `relay_core/config.py`; the
`relay_core/observability/` package is kept as an empty placeholder only so the
repository layout still matches docs/system-design.md section 24, with no code planned
against it.

## Consequences

- No LLM call tracing/cost dashboarding beyond whatever is stored directly in Postgres
  (`llm_calls`, `agent_runs.cost_usd`, etc. per section 14.3) — those tables remain the
  source of truth for cost and usage; there's just no external trace viewer over them.
- No distributed request tracing across FastAPI/Celery/Redis/SQLAlchemy.
- No automatic crash reporting; unhandled exceptions surface only in process logs
  (stdout/CloudWatch/wherever the container logs go), not in a dedicated dashboard with
  alerting.
- The eval harness (section 21) and its CI gates are unaffected — evals read/write
  `eval_*` tables directly and don't depend on Langfuse.
- If any of this is wanted later (e.g., Sentry specifically is low-effort to add — one
  SDK init call and a free-tier DSN), it can be reintroduced without touching anything
  else; nothing else in the design depends on these three being present.
- `docs/system-design.md` itself is left as originally written; this ADR is the record
  of the descope, consistent with ADR-0005/0006/0007.

## Alternatives considered

- Keep Sentry only, cut Langfuse + OTel: considered, since Sentry is genuinely low
  effort — but the user asked to cut "the entire observability part," so all three are
  cut together for now. Revisit Sentry specifically if debugging without it becomes
  painful.
