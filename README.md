# Relay — Enterprise AI Operations Agent

A multi-tenant, pluggable agentic platform. Companies connect whatever tools they have
(databases, documents, email, CRM, web search, code execution, or any MCP server / REST API),
and Relay plans, executes, validates, and — with human approval — acts on multi-step
operational tasks. Powered by Google Gemini.

Full design: [docs/system-design.md](docs/system-design.md).

## Status

**Phase 3** (connector framework + first connectors + eval harness): a real `Connector`
interface/registry/manifests, envelope-encrypted credentials, a tool registry + executor with
untrusted-output wrapping, and the full `plan -> check_capabilities -> execute_step ->
validate_step -> next_step -> synthesize` step-execution loop. The `postgres` connector (with a
`sqlglot`-based read-only SQL guard) plus a seeded demo Postgres database, and an eval harness
CLI (`relay-eval`) covering the `routing`, `capability_detection`, `text_to_sql`, and `rag`
suites as a blocking CI gate (`.github/workflows/ci.yml`'s `eval-harness` job), alongside
dedicated tests for the connector framework itself (`backend/tests/{unit,integration}/`). See
[docs/adr/0009-phase3-connector-metadata-in-code.md](docs/adr/0009-phase3-connector-metadata-in-code.md)
for what's deliberately deferred to later phases.

**Phase 4** (knowledge & sandbox): a RAG pipeline — TXT/MD/DOCX/PDF parsers (with a
Gemini-transcription fallback for scanned PDFs), a structure-aware chunker, hybrid retrieval
(pgvector cosine + Postgres full-text, fused with RRF, reranked by Flash-Lite), and a
`documents` connector/knowledge-base upload API on top of it. A sandbox service (`sandbox/`)
that runs `code.execute` in a fresh, network-isolated, resource-limited container per call, and
a `python_sandbox` connector that can resolve an earlier tool call's output by
`ref://tool_call/<id>` handle as an input and upload chart/CSV outputs as artifacts.
`file_upload`, `documents`, and `python_sandbox` are all always-available (no installation row),
the same way — see each connector's own docstring. Demo content
(`demo/documents/`, ingested by `make seed`) and eval fixtures (`evals/fixtures/`) are two short
Markdown policy documents sharing the same account names/thresholds as the seeded SQL data.

See [docs/system-design.md §28](docs/system-design.md#28-implementation-plan-week-by-week) for
the full ten-week implementation plan. The chat UI and every other frontend page are a separate,
upcoming pass so they get proper design attention rather than a placeholder look — everything
above is backend-only.

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
