# Phase 7: Real integrations, memory, replanning (status)

**Last updated:** 18 September 2026
**Design doc:** [system-design.md](system-design.md) §8.2–8.5, §10.3–10.4, §12, §14.3, §15.3/§15.5, §18.3, §19.1, §21.1, §28 Phase 7
**Tickets:** [phase-7-tickets.md](phase-7-tickets.md)
**Working tree:** A1–D4 are committed (HEAD is `98a530d`). E1, F1 and F2 are **uncommitted**.

Phase 7 is complete: all 15 tickets are done. **Done when (§28):** the demo runs against real
Gmail drafts and real Google Calendar events through a test-mode Google OAuth app, the agent
remembers a stated preference across conversations, and a failed step is replanned instead of
being reported as a dead end.

Two of those three are proven in the test suite. The Google half is proven against a mock that
now speaks Google's own REST shapes and demands a bearer token — **it has not been run against
real Google**, which needs a Cloud console OAuth client nobody has created yet. See "Not yet
verified". §28's "and a real HubSpot test account" is out of scope per
[adr/0006](adr/0006-hubspot-dropped-from-scope.md).

---

## Done: 15 of 15 tickets

### Workstream A: real Google integrations

| Ticket | Summary |
| ------ | ------- |
| **A1** | `mocks/main.py` answers the real Gmail (`/gmail/v1`) and Calendar (`/calendar/v3`) endpoints: ids-only message lists with `from:`/`subject:` `q` parsing, `format=metadata\|full`, base64url RFC 5322 drafts, `freeBusy`, and Google's own 401 envelope for a missing bearer. Plus `/oauth/token` for both grants, so A4 is testable without Google. |
| **A2** | One migration for both Phase 7 schema changes: the `memories` table (§14.3, with its scope/user check constraint) and `connector_credentials.oauth_expires_at`. `MemoryRepository` with `nearest`/`mark_used`, and `list_expiring_across_workspaces` on the credential repository. |
| **A3** | `GET /connectors/{id}/oauth/start` (admin only) and the unauthenticated `GET /oauth/callback`. `state` is signed with the existing JWT key; the PKCE verifier lives in Redis under a nonce and never travels in the URL. A successful callback stores the token, health-checks, syncs tools and redirects. |
| **A4** | `ensure_fresh_token`: one refresh implementation, called lazily when secrets are loaded for a call and from a five-minute beat over `now + 15 min`. `invalid_grant` marks the installation `degraded` naming reconnection; a network blip does not. |
| **B1** | `gmail` speaks Gmail: `search_emails` lists ids then fetches each with `format=metadata` (capped at 10, concurrent), `get_email` walks `payload.parts`, `create_draft` builds the message with stdlib `EmailMessage` and base64url-encodes it. `blocked_recipients` still enforced before the request is built. |
| **B2** | `google_calendar` speaks Calendar: `list_events` with `singleEvents`/`orderBy`, `find_free_slots` over `freeBusy` blocks, `create_event` with explicit `timeZone` and `sendUpdates` defaulting to `none`. |

### Workstream C: closing the graph

| Ticket | Summary |
| ------ | ------- |
| **C1** | `replan` node (`PLANNER` profile). `validate_step`'s `replan` verdict routes here instead of being treated as `fail`; `replan` edges to `check_capabilities`, so a revision needing something absent lands on the existing missing-capability card. Done steps are preserved in code, dangling `depends_on` ids are pruned, bounded at 2. Emits `plan.updated`. |
| **C2** | `validate_final` (`VALIDATOR` profile) checks the draft against the step results and `sources`. `synthesize` buffers into `draft_chunks` instead of streaming; the validator publishes them only on the pass that reaches `finalize`. One revision, then the draft ships regardless. Skipped on `direct_answer` and `blocked`. |

### Workstream D: memory

| Ticket | Summary |
| ------ | ------- |
| **D1** | `relay_core/memory/extract.py` + `relay_worker/tasks/memory.py` on the `memory` queue. Rules in code: confidence ≥ 0.75, a secret-shape regex, embedding dedup at cosine similarity > 0.92, workspace scope downgraded below admin. Enqueued from `finalize` through a dispatcher that waits for the run's transaction to commit. |
| **D2** | `load_context` embeds the message, takes the top 5, calls `mark_used`. New `memories: list[str]` on `AgentState`. `relay_core/agent/context.py` renders one block for the `plan`, `execute_step`, `direct_answer` and `synthesize` prompts. An embedding failure leaves the run working. |
| **D3** | `GET/PATCH/DELETE /workspaces/{ws}/memories[/{id}]` with `kind`/`scope`/`q` filters. Editing content re-embeds. A member manages their own; only an owner or admin may change a workspace-scope memory. Another member's memory is 404, not 403. |
| **D4** | `relay_core/memory/summarize.py`: everything past the watermark except the last 8 messages is folded into `conversations.summary`, with `summary_upto_message_id` moved in the same write. Re-summarizing folds the previous summary in. Triggered inline from `finalize` past 20 pending messages. |

### Workstream E: resilience

| Ticket | Summary |
| ------ | ------- |
| **E1** | `relay_core/connectors/breaker.py`: Redis-backed, 5 failures in 60 s opens for 120 s. Recorded from `ToolExecutor`'s `except` branch only. Opening sets `health = degraded`; a successful call or health check clears it. `capabilities.resolver.drop_tripped` filters tripped installations out of binding for both the resolver and the registry. A second beat re-checks only `degraded`/`down` installations every 10 minutes. |

### Workstream F: proof and docs

| Ticket | Summary |
| ------ | ------- |
| **F1** | New `planning` suite (gate 0.9, 2 cases) scored on `expect_replan`, counted from `llm_calls` rows with `node = 'planner'`. A groundedness case and a two-run memory case in `task_success`, ordered by a new `depends_on_case` key. The harness extracts memories inline, since there is no Celery worker behind it. |
| **F2** | [adr/0013](adr/0013-real-oauth-memory-and-replanning.md), this note, the README Phase 7 paragraph. `.env.example` already documented the connector-OAuth reuse and the redirect URI when A3/A4 landed; Phase 7 D–F added no settings. |

**Tests:** 162 unit, 214 integration. `ruff` and `mypy` are clean.

```bash
cd backend
pip install -e ".[dev]" -e ../evals/relay_eval
python -m pytest tests/unit -q
python -m pytest tests/integration -q            # needs a running Docker daemon
python -m ruff check relay_core relay_api relay_worker tests alembic
python -m mypy relay_core relay_api relay_worker
```

`tests/integration/test_eval_approval_compliance.py` imports `relay_eval`, so the eval package
must be installed (or on `PYTHONPATH`) for the integration suite to collect.

Three checks were confirmed to go red against the code they guard:

- `test_eval_planning_gate.py` scores two real runs that hit the same dead end — one that replans
  and one that does not — and asserts the second fails. `expect_replan` is a proxy (two
  `planner` calls), so this is what stops the `planning` gate from being unfailable.
- `test_validate_final.py` asserts no `token` event ever carried text from a rejected draft. It
  fails against a `synthesize` that streams.
- `test_circuit_breaker.py` asserts the sixth call never reaches the connector, not merely that
  the health column changed.

---

## Not yet verified

- **Real Google.** Nothing here has touched Google. A1's mock now demands a bearer token and
  answers Google's shapes, and A3's callback test points `token_uri` at the mock's
  `/oauth/token`, so no network call leaves the test suite. Creating a test-mode OAuth client,
  registering `{API_BASE_URL}/api/v1/oauth/callback`, and running one real connect → draft →
  event cycle is the remaining work on §28's "done when". Expect the first real run to surface
  scope and consent-screen problems, not code problems.
- **Real-Gemini evals.** The `planning` suite, the `task_success` groundedness case and the
  memory pair have never been run. They need `GEMINI_API_KEY`, the mock service and the demo
  database. Whether the two `planning` cases actually provoke a replan against a real model is
  unknown — they are written to provoke one (a filter that matches nothing, a page that 404s),
  but a model is free to report the dead end honestly and still fail the case. If they do, fix
  the cases before touching the gate.
- **The memory eval pair is not hermetic.** `task_success` shares one persistent `eval-full`
  workspace across harness runs, so a memory written by the setup case survives into later runs.
  A second run of the suite would pass `memory_honours_preference` from a stale memory even if
  extraction were broken. The `depends_on_case` ordering is right; the isolation is not.
- **The Docker stack.** The new `recheck-unhealthy-connectors` beat entry and the `memory` queue
  having a real consumer have not been run against `docker compose`.
- **Conversation summarization at scale.** The threshold is a message count, not the §12.1 token
  count, and no conversation in any test has passed 50 messages.

---

## Decisions made that aren't in the design doc

The seven Phase 7 decisions are in [adr/0013](adr/0013-real-oauth-memory-and-replanning.md). What
follows is the smaller stuff that still surprised someone.

1. **`validate_step`'s `replan` verdict is the only entry to replanning**, and `replan_reason` on
   `AgentState` is both the explanation and the routing signal. The graph edge reads it; `replan`
   clears it. §8.2's `approval_gate --> replan` edge is deliberately not built (ADR-0013 §4).
2. **A revision that adds no new step is a failure, not a retry.** The step stays `failed`,
   `next_step` cascades the skip, and `synthesize` explains the gap — exactly the behaviour that
   existed before C1.
3. **`replan` prunes `depends_on` ids that no longer exist.** A revision usually points a new step
   at the id of the step that just failed, and `next_step` only runs a step once every dependency
   is `done`, so a dangling id would park it forever.
4. **Memory extraction is enqueued through SQLAlchemy's `after_commit` event**, not an outbox
   table. `finalize` runs inside the run's transaction, and extraction refuses to run on a run
   that is not yet `completed`.
5. **Summarization is awaited inline in `finalize`; extraction is queued.** Extraction runs after
   every completed run; summarization fires on roughly one turn in twenty, so a queue hop would be
   more machinery than the work it defers.
6. **`summarize_conversation` takes repositories, not a session**, because its one in-graph caller
   is a node, and no node in `relay_core/agent/nodes/` has a session.
7. **`supersedes_id` is load-bearing.** The extractor is shown the user's existing memories with
   their ids, so it can say "this replaces that" for a preference that *changed* rather than one
   merely restated. Embedding dedup handles the restated case.
8. **A member's run cannot overwrite a workspace-scope memory**, by `supersedes_id` or by being
   close enough to it in embedding space. Their observation is stored as their own instead.
9. **The breaker filter runs over rows, not inside `list_bindable`'s SQL.** The ticket says
   `list_bindable` drops the installation; breaker state is in Redis and the query is in Postgres,
   so one `MGET` over the candidates is cheaper than teaching the repository about Redis. The
   behaviour is what the ticket asked for; the seam is one layer out.
10. **The recovery beat reuses the six-hourly sweep's body** with a different candidate list
    (`list_unhealthy_across_workspaces`), rather than being a second implementation of "health
    check, then sync".
11. **A successful health check clears the breaker.** Without that, an installation that recovered
    would be taken out of binding again by its next two failures.
12. **`llm_calls.node` records the gateway `role`,** so `plan` and `replan` both write
    `node = 'planner'`. F1's `expect_replan` counts those rows rather than adding a column.
13. **`gemini_rpm_limit` is raised to 100,000 in `tests/conftest.py`.** The limiter's window is
    keyed by model and minute in one shared Redis database, so the whole suite competed for the
    production default of 60 and the Phase 7 tests tipped unrelated flow tests into
    `RateLimitExceeded`.
14. **`ScriptedModels` records `systems` and `contents`, and answers `embed_content`.** A memory
    that never reaches a prompt has changed nothing, and without the embed stub every flow test
    would take `load_context`'s retrieval-failure path.

---

## Known rough edges

- **Memory retrieval marks a memory used the moment it is injected**, not when it changes the
  answer. `use_count` measures what a run was told, not what it used.
- **`mark_used` writes on every run with a matching memory.** One `UPDATE` per run, on a table
  that is otherwise read-mostly. Fine at this size; a candidate for batching if it isn't.
- **The secret-shape regex is deliberately broad** and will drop a legitimate memory containing
  the word "password" or a 40-character base64-looking string. A dropped memory costs nothing; a
  stored key is a breach.
- **Summarization reads at most 200 messages per pass.** A conversation whose watermark falls
  outside that window re-summarizes content it has already covered. The 20-message trigger keeps
  the watermark well inside it in practice.
- **`validate_final` sees step summaries and `sources`, not raw tool output.** A number that was
  in a tool result but never made it into a step summary reads as unsupported, so the validator
  can ask for a revision that cuts a true claim. Erring that way is the right direction.
- **The breaker is per-installation, not per-tool.** One failing tool takes the whole installation
  out of binding for two minutes.
- **The breaker's fixed window shares `relay_core.llm.ratelimit`'s edge behaviour:** five failures
  spread either side of a minute boundary don't trip it.
- **Marking an installation degraded from `ToolExecutor` writes through the run's session**, so it
  lands when the run's transaction commits, not immediately. A run that later fails and rolls back
  loses the health message; the Redis breaker state survives regardless, so binding is still
  correct.
- **No frontend.** The memory page (`/w/[slug]/memory`), the **Connect with Google** button and
  the `?connected=1` / `?error=` states, and `plan.updated` / revision notices in the run timeline
  are all deferred to the frontend pass, as the tickets specify. The backend routes above are the
  whole contract those pages need.
