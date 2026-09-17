# ADR-0012: Tool definitions and bring-your-own tools

## Status

Accepted. Supersedes the `tool_definitions` part of
[ADR-0009](0009-phase3-connector-metadata-in-code.md). ADR-0009's other deferrals still stand:
there is no `connector_definitions` table and no `capability_bindings` table.

## Context

Phase 6 (docs/system-design.md section 28) lets a workspace plug in tools Relay has never seen:
any remote MCP server, or operations picked from an OpenAPI spec. The "done when" is that the
sample MCP server can be installed and the agent uses it with no code changes.

Until Phase 6, `ToolRegistry` asked each connector for its tools on every run, and a connector's
manifest declared which capabilities it provided. That model can't serve discovered tools:

- There is nowhere to keep an admin's decision about one tool: enabled or disabled, a risk
  override, the capabilities it serves.
- There is nothing to compare against, so an MCP server could change what a tool does between
  two runs and nobody would notice.
- A manifest is written before discovery, so it can't know an MCP server's capabilities.
- Every installation of the same type provides everything its manifest lists, so installation
  priority can't pick between two providers.

## Decision

### 1. Build `tool_definitions` now. Still no `connector_definitions` or `capability_bindings`.

Manifests stay in code (ADR-0009). Priority stays on `connector_installations.priority`, but it
now does something: for each requested capability, the registry binds only the tools of the
best-priority installation that provides it (ties go to the newest installation).

### 2. Every installed connector gets rows, not only MCP and OpenAPI

This covers postgres, gmail, google_calendar, web_search, mcp and openapi. Per-tool
enable/disable, risk override (FR-7, FR-8) and retrieval embeddings then work the same way for
every tool. `relay_core.tools.sync.sync_installation` writes the rows on install, on
`POST /connectors/{id}/test` and `/sync`, from `ensure_installation`, and every six hours from
Celery beat.

The always-available connectors (`file_upload`, `documents`, `python_sandbox`) have no
installation row. They keep binding live from code and are never ranked out by retrieval.

### 3. Only `mcp` tools go through schema-change review

Sync hashes each tool's description and input schema. If an MCP tool's hash changes, the row is
updated, **disabled**, and marked `needs_review`, so a tool an admin approved can't quietly start
doing something else (section 18.1, "Malicious MCP server"). Built-in specs are trusted code, so
a changed built-in is updated silently. OpenAPI operations are frozen in the installation's
config at install time, so they never change upstream.

### 4. Low confidence means review, not disabled

The capability tagger proposes capabilities, a risk and a confidence for tools that declare no
capabilities. Code keeps only taxonomy keys and well-formed `custom.<domain>.<action>` keys,
clamps confidence, and only ever raises risk above the connector's default. A tool below 0.7
confidence, or with no capabilities, is marked `needs_review` but stays enabled.

This is safe for two reasons:

- Every MCP tool starts as a `write`, so every call stops for approval until an admin lowers it.
  An MCP server's `readOnlyHint` reaches the tagger only as a hint; a server's claim about its
  own tool never lowers its risk.
- A tool with no capabilities is never bound, so an untagged tool does nothing until someone
  tags it.

### 5. Custom capabilities reach the planner

The resolver reads capabilities from the enabled rows of healthy installations, so `custom.*`
keys become available. The planner prompt lists the workspace's available custom capabilities
next to the taxonomy. Without this, an MCP ticketing tool would have no key the planner is
allowed to plan against.

### SSRF allow-list for dev hosts

Every user-supplied URL (MCP server URLs, OpenAPI spec and base URLs, `fetch_url`) goes through
`relay_core.security.ssrf`. The guard resolves the host on every request, rejects any
non-public address, and connects to the address it checked, so DNS rebinding can't slip past it.
The `mcp` SDK is built on `httpx2`, a fork with its own classes, so the guard provides the same
transport for both `httpx` and `httpx2`.

The dev stack's own services (`mock-services`, `mcp-ticketing`) resolve to private addresses, so
`SSRF_ALLOWED_HOSTS` exempts exact hostnames from the address check. It never exempts them from
the scheme check, which allows `http` only when `ENV=dev`. Production leaves the list empty.

## Consequences

- An existing database needs one `make sync-tools` before its installed connectors bind again.
  Fresh installs and `make seed` create their rows themselves.
- Installing a connector calls Gemini: the tagger for tools with no capabilities, and embeddings
  for every new or changed tool. Both failures are tolerated; the tools are just left untagged
  or unranked. The install and sync routes therefore depend on the Gemini client, so they fail
  without a `GEMINI_API_KEY`.
- Risk and enablement can change while an approval is pending. `approval_gate` matches approved
  rows to calls by the approval's own `proposed_args`, not by re-checking risk. A held call whose
  tool became a write in the meantime doesn't run.
- The six-hourly sweep runs each installation's health check before syncing it, so an
  installation that was down and recovered is picked up again automatically.
- `ConnectorInstallationRepository.list_active_across_workspaces` is a second deliberate
  cross-tenant query, for that sweep.

## Alternatives considered

- **Keep discovery live, with no table.** Rejected: nowhere to keep review state or overrides,
  and nothing to detect a changed tool against.
- **Rows only for MCP and OpenAPI tools.** Rejected: two binding paths, and built-in tools would
  have no per-tool enable/disable or risk override.
- **Disable every low-confidence tool.** Rejected: the write default already gates every call,
  and disabling would make a fresh MCP install useless until someone reviewed every tool.
- **A `capability_bindings` table for per-capability priority.** Deferred: installation priority
  covers every current case. Add it when one installation must be preferred for one capability
  but not for another.
