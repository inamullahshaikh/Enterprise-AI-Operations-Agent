# Relay — Enterprise AI Operations Agent

A multi-tenant, pluggable agentic platform. Companies connect whatever tools they have
(databases, documents, email, CRM, web search, code execution, or any MCP server / REST API),
and Relay plans, executes, validates, and — with human approval — acts on multi-step
operational tasks. Powered by Google Gemini.

Full design: [docs/system-design.md](docs/system-design.md).

## Status

Phase 1 (foundation: auth, workspaces/RBAC, LLM gateway) — see
[docs/system-design.md §28](docs/system-design.md#28-implementation-plan-week-by-week)
for the full ten-week implementation plan.

## Repository layout

| Path | Purpose |
|------|---------|
| `backend/` | FastAPI API (`relay_api`), Celery workers (`relay_worker`), and the shared core library (`relay_core`) |
| `frontend/` | Next.js web app |
| `sandbox/` | Ephemeral Python execution service used by the `code.execute` capability |
| `mocks/` | Mock Gmail / Calendar / Search servers for dev and evals |
| `mcp_examples/` | Sample MCP server used to demo bring-your-own-tools |
| `evals/` | Evaluation suites, fixtures, judge rubrics, and the eval harness CLI |
| `demo/` | Seed data for the demo company, sample documents, and CSVs |
| `infra/` | Terraform modules and environments (`portfolio`, `reference`) |
| `docs/` | System design doc and architecture decision records |

## Quickstart (local development)

```bash
cp .env.example .env      # fill in GEMINI_API_KEY and R2_* at minimum
make up                   # docker compose up (postgres, redis, api, worker, web, ...)
make migrate               # run Alembic migrations
make seed                  # seed the demo company data
make test                  # run backend + frontend test suites
```

See [docs/system-design.md](docs/system-design.md) for architecture, data model, security model,
and the evaluation framework.
