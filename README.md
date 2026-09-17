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

**Phase 5** (writes & approvals): the first connectors that change things outside Relay —
`gmail` (draft-first: composing and sending are separate approvable writes) and
`google_calendar`, both backed for now by the mock service in `mocks/`. A policy engine decides
in code, not in the prompt, which calls stop for a human. The run parks at LangGraph's
`interrupt()`, and a decision through `POST /approvals/{id}/decision` resumes it from its
checkpoint in any worker. Batches can be approved in part, with arguments edited per item.
Undecided approvals expire after 24 h. Approved writes carry idempotency keys, and their
`tool_calls` row is committed straight away, so a worker killed mid-resume never sends twice.
`relay-eval` now parks, decides, and resumes write cases. Its `approval_compliance` suite fails
CI on any write that runs without approval, and `task_success` runs the end-to-end renewal
scenario. `make seed` installs both mock-backed connectors in the demo workspace. See
[docs/adr/0011-approvals-interrupt-resume-and-enforcement-in-code.md](docs/adr/0011-approvals-interrupt-resume-and-enforcement-in-code.md).

**Phase 6** (bring your own tools): plug in any remote MCP server, or pick operations from an
OpenAPI 3 spec, and the agent can plan with and call those tools with no code changes. Every
installed connector's tools now live in a `tool_definitions` table, so admins can enable or
disable a tool, override its risk and assign capabilities through `/tools`. An LLM tagger
proposes capabilities for discovered tools, with code enforcing the rules; the most important is
that it can only raise risk. `/capabilities` shows which installation serves each capability,
and installation priority picks the winner. A changed MCP tool is disabled until someone reviews
it, and discovery re-runs every six hours. Every user-supplied URL goes through an SSRF guard
that pins each connection to the address it checked. Tool retrieval by embedding keeps large
workspaces to 20 tools per call. There is also a `web_search` connector (Tavily-shaped, with
guarded `fetch_url`) and a sample MCP ticketing server in `mcp_examples/`. See
[docs/adr/0012-tool-definitions-and-byo-tools.md](docs/adr/0012-tool-definitions-and-byo-tools.md)
and [docs/phase-6-status.md](docs/phase-6-status.md). An existing dev database needs
`make sync-tools` once after upgrading.

Phase numbers here follow the design doc. The commit history runs one behind: the commit titled
"Phase 3 completed" holds both Phase 3 and Phase 4.

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
