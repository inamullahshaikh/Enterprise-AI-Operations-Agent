# Phase 6: Bring your own tools (status)

**Last updated:** 17 September 2026
**Design doc:** [system-design.md](system-design.md) §6.4–6.7, §7.4, §10.5, §14.3, §15.3, §17.3, §18.5, §28 Phase 6
**Tickets:** [phase-6-tickets.md](phase-6-tickets.md)
**Working tree:** A1–A4 are committed (HEAD is `0d40aaf`). Everything from B1 on is **uncommitted**.

Phase 6 is complete: all 16 tickets are done. **Done when (§28):** the sample MCP server can be
plugged in and the agent uses it with no code changes. `test_byo_mcp_flow.py` proves this through
the API with a scripted model, and it imports nothing ticketing-specific from `relay_core`.

---

## Done: 16 of 16 tickets

### Workstream A: foundation

| Ticket | Summary |
| ------ | ------- |
| **A1** | SSRF guard: an async DNS check on every request (redirects included), connecting to the checked address, a 2 MB body cap, and `SSRF_ALLOWED_HOSTS` for dev services. |
| **A2** | `tool_definitions` table and repository, plus `connector_installations.last_synced_at`. |
| **A3** | `sync_installation`: upserts rows from `list_tools`, with Gemini-safe `llm_name`s, a `POST /sync` route, a backfill from `ensure_installation`, and `make sync-tools`. |
| **A4** | The registry and the resolver read rows. Only the best-priority installation binds per capability. Executor timeouts, a 20,000-character cap on the output the model sees, and `custom.*` capabilities in the planner prompt. |

### Workstream B: bring-your-own connectors

| Ticket | Summary |
| ------ | ------- |
| **B1** | `mcp_examples/ticketing`: an `mcp` SDK `MCPServer` with 4 tools, optional bearer auth, `/_reset` and `/_stats`, a Dockerfile and a compose service on port 8200. `mcp_ticketing` test fixture. |
| **B2** | `mcp` connector over streamable HTTP through an `httpx2` variant of the SSRF guard. Every tool is `write` with no capabilities, and `readOnlyHint` is kept only as a tagger hint. Install validation returns 400 for a blocked URL. |
| **B3** | A changed MCP tool is disabled and sent to review. Celery beat runs `sync-connector-tools` every 6 h, with a health check first and per-installation error isolation. The worker now listens on the `connectors` queue. |
| **B4** | `openapi_spec.py`: OpenAPI 3.x only, local `$ref`s inlined with a cycle cut-off, and one operation per method and path. `POST /connectors/openapi/preview` takes a pasted spec or a URL fetched through the guard. `openapi-pydantic` removed. |
| **B5** | `openapi` connector: operations live in installation config, risk comes from the HTTP method, path arguments are fully percent-encoded, and `Idempotency-Key` is forwarded. |
| **B6** | `web_search` connector (Tavily-shaped `search_web`, guarded `fetch_url` with HTML-to-text). Mock `/search` and `/pages/{slug}`. Manifest `default:` values are applied at install. `WEB_SEARCH_*` settings removed. |

### Workstream C: tagging, review, retrieval

| Ticket | Summary |
| ------ | ------- |
| **C1** | Capability tagger: one `LIGHT` call per 25 tools. Code validates capability keys, clamps confidence, and only raises risk. Runs from sync for new or changed tools that declare no capabilities and aren't admin-assigned. |
| **C2** | `GET/PATCH /tools`, `GET /capabilities` (providers, winner, gaps), `PATCH /connectors/{id}` (priority, status, name). `approval_gate` matches approved rows by the approval's own `proposed_args`. |
| **C3** | Sync embeds new, changed or unembedded rows. `tools_for_run(query, limit=20)` keeps the nearest installed tools with `NULLS LAST`. `execute_step` passes the step goal; `approval_gate` passes no query. |

### Workstream D: proof and evals

| Ticket | Summary |
| ------ | ------- |
| **D1** | `test_byo_mcp_flow.py`: install MCP → scripted tagger → admin lowers `search_tickets` to read → `custom.ticket.read` available → a read run completes against the real server → `create_ticket` parks for approval, with 0 tickets created until it is approved. |
| **D2** | The `full` eval profile adds `web_search` (mock) and `mcp` (ticketing, via the new `MCP_TICKETING_URL`) and resets and counts both services. New `tool_selection` suite (gate 0.9, 3 cases) and `approval_compliance/mcp_create_ticket`. The CI eval job starts the ticketing server. |

### Workstream E: docs

| Ticket | Summary |
| ------ | ------- |
| **E1** | [adr/0012](adr/0012-tool-definitions-and-byo-tools.md), this note, the README Phase 6 section, `.env.example`, `mcp_examples/README.md`, `evals/README.md`. |

**Tests:** 162 unit, 143 integration. `ruff` and `mypy` are clean.

```bash
cd backend
pip install -e ".[dev]" -e ../evals/relay_eval   # mcp>=2.2,<3 is new
python -m pytest tests/unit -q
python -m pytest tests/integration -q            # needs a running Docker daemon
python -m ruff check relay_core relay_api relay_worker tests alembic
python -m mypy relay_core relay_api relay_worker
```

Three tests were checked by breaking the code they guard, to make sure they go red:

- The mid-approval risk-change regression fails against the Phase 5 `approval_gate`.
- The OpenAPI path-traversal test fails when path arguments aren't encoded (the call reaches `POST /_reset`).
- The MCP rug-pull test depends on the hash check that disables the changed tool.

---

## Not yet verified

- **Real-Gemini evals.** `relay-eval run --suite tool_selection` and the new
  `approval_compliance/mcp_create_ticket` case need `GEMINI_API_KEY`, the mock service, and the
  ticketing server. They have not been run. Whether adding BYO tools to the `full` profile keeps
  `task_success/renewals_001` above its gate is also unknown. If it drops, split BYO into its own
  profile rather than loosening the gate.
- **The Docker stack.** The `mcp-ticketing` image, the worker's `connectors` queue, the beat
  entry and `make sync-tools` have not been run against `docker compose`.
- **A real Tavily key** against `web_search`. Only the mock has been exercised.

---

## Decisions made that aren't in the design doc

1. **`mcp` 2.x.** The installed SDK is `mcp` 2.2, where FastMCP is `MCPServer` and the client
   runs on `httpx2`. `pyproject.toml` pins `mcp>=2.2,<3`. The SSRF guard exposes
   `guarded_mcp_http_client`, a pinned `httpx2` client, so there is no rebinding gap for MCP.
2. **`Connector.validate_config`** is a new optional hook. The install route turns a
   `ValueError` or `SSRFBlocked` from it into a 400 before anything is written. `mcp` and
   `openapi` use it.
3. **`ToolSpec.read_only_hint`** carries an MCP server's hint to the tagger. Nothing else reads it.
4. **A tagger-assigned risk survives a re-sync.** Resetting it to the connector's default would
   lower it; only a new tag or an admin override changes it.
5. **Installing needs the Gemini client.** Install, test and sync routes depend on
   `get_llm_gateway`. Integration tests override `get_genai_client` with a client that fails
   every call, so sync runs its untagged, unembedded path.
6. **The sweep checks health before syncing** and doesn't change `status`. A transient failure
   no longer leaves an MCP installation down until someone presses "test".
7. **A held call whose tool became a write mid-approval doesn't run.** It returns "now needs
   approval" to the model. An approved call whose tool was disabled or unbound is marked
   `skipped` with "no longer available".
8. **A capability-map provider with `winner: true` and no `installation_id`** is an
   always-available source (`file_upload`, or `documents` once something is ingested). It binds
   alongside the installed winner rather than competing with it.
9. **OpenAPI path arguments equal to `.` or `..` are refused.** Percent-encoding doesn't encode
   dots, and a client would collapse such a segment.
10. **Eval case messages may use `{mock_services_url}`**, because the page URL differs between
    Docker and CI.

---

## Known rough edges

- `code.execute` is bound by the registry but never reported as available by the resolver (this
  predates Phase 6). A plan that requires it is reported missing.
- Installing any connector returns 500 when `GEMINI_API_KEY` is empty, because the gateway
  dependency builds the Gemini client (decision 5).
- A capability edited through `PATCH /tools` clears the row's embedding. It is re-embedded on the
  next sync, and until then the tool sorts last in retrieval.
- One MCP session per call, and a static bearer token only (skipped by B2; see the ticket).
- `seed_demo` installs without a gateway, so demo tools get embeddings only on the first sweep or
  `make sync-tools`.
- The frontend screens (install wizard, operation picker, tool review, capability map) are
  deferred to the frontend pass, as for the Phase 5 approvals UI.
