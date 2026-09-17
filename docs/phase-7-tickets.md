# Phase 7: Real integrations, memory, replanning (tickets)

**Design doc:** [system-design.md](system-design.md) §8.2–8.5 (`replan`, `validate_final`), §10.3–10.4 (Gmail, Calendar),
§12 (memory), §14.3 (`memories`, `connector_credentials`), §15.3/§15.5 (OAuth and memory routes), §18.3 (credential
encryption, token refresh), §19.1 (circuit breakers), §28 Phase 7
**Builds on:** [phase-6-status.md](phase-6-status.md), [adr/0011](adr/0011-approvals-interrupt-resume-and-enforcement-in-code.md), [adr/0012](adr/0012-tool-definitions-and-byo-tools.md)

**Done when (§28, restated):** the demo runs against **real Gmail drafts and real Google Calendar events** through a
test-mode Google OAuth app, the agent remembers a stated preference across conversations, and a failed step is
replanned instead of being reported as a dead end.

§28 also says "and a real HubSpot test account". HubSpot was dropped in
[adr/0006](adr/0006-hubspot-dropped-from-scope.md), so the CRM half of that sentence is out of scope and the Google
half is the whole bar. Per the backend-first rule, "from the UI" still means "through the API" in this phase; the
memory page and the OAuth connect button come in the frontend pass (see the end of this file).

---



## How to use this file

Work through the tickets in the order below. Each ticket leaves `ruff`, `mypy` and both test suites green.

```bash
cd backend
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m ruff check relay_core relay_api relay_worker tests alembic
python -m mypy relay_core relay_api relay_worker
```

| #   | Ticket                                                          | Depends on |
| --- | --------------------------------------------------------------- | ---------- |
| 1   | **A1** Mock services speak the real Google APIs                 | none       |
| 2   | **A2** `memories` table, `oauth_expires_at`, repository         | none       |
| 3   | **A3** Connector OAuth flow (start, callback)                   | A2         |
| 4   | **A4** Token refresh, lazily and on a schedule                  | A3         |
| 5   | **B1** `gmail` against the real Gmail API                       | A1, A4     |
| 6   | **B2** `google_calendar` against the real Calendar API          | A1, A4     |
| 7   | **C1** `replan` node                                            | none       |
| 8   | **C2** `validate_final` groundedness check                      | none       |
| 9   | **D1** Memory extraction after a run                            | A2         |
| 10  | **D2** Memory retrieval in `load_context`                       | D1         |
| 11  | **D3** Memories API                                             | A2         |
| 12  | **D4** Conversation summarization                               | none       |
| 13  | **E1** Circuit breaker + installation health monitoring         | none       |
| 14  | **F1** Evals: `planning` suite, groundedness and memory cases   | C1, C2, D2 |
| 15  | **F2** ADR-0013, status note, README, `.env.example`            | all        |

A1 comes first because it decides the shape every later Google ticket writes against. A2 lands both of this phase's
schema changes in one migration so there is only one Phase 7 revision to apply.

---



## Decisions carried into this phase

These choices shape the tickets below. F2 records them in an ADR.

1. **One code path per connector, not a "mock mode" branch.** The mock service is rewritten to emulate the real
  Gmail and Calendar REST shapes (A1), and the connectors always speak Google. Pointing an installation at the mock
   is then only a `config.base_url` difference, exactly as it is today for `web_search` against Tavily. A connector
   with `if mock:` branches would drift, and the branch used by every eval would be the one that is never the real one.
2. **Tool names, arguments and risks do not change.** Only the HTTP underneath moves. Every Phase 5 and Phase 6
  approval, eval case and `tool_definitions` row keeps working, and `schema_hash` stays stable so nothing is sent
   to review.
3. **OAuth tokens live in the existing encrypted secrets blob**; only the expiry gets a column
  (`connector_credentials.oauth_expires_at`, §14.3). The refresh sweep has to find expiring installations without
   decrypting every credential in the database, and that is the one field it needs in the clear.
4. **A rejected approval still comes back to the model as a tool result** (ADR-0011 decision 5), *not* straight to
  `replan`. §8.2 draws an `approval_gate --> replan` edge, but the rejection has to be answered as a function
   response for the turn to be valid at all, and the step's own validator is a better judge of whether the plan is
   now unworkable than the gate is. `validate_step` reaching a `replan` verdict is the single entry to replanning.
5. **`synthesize` stops streaming its first draft when a validator follows it.** A groundedness check that can
  demand a revision is worthless if the unvalidated text is already on the user's screen. C2 buffers the draft,
   validates, and streams the text that passed.
6. **Memory is off unless the workspace says otherwise.** `workspace_policies.memory_enabled` already exists and
  defaults to true, but extraction and retrieval both check it, so a workspace can turn the feature off without a
   deploy.
7. **A breaker-open installation is treated exactly like an unhealthy one.** It stops binding, the resolver reports
  its capabilities as provided by the next installation in priority order, and nothing new is needed in the planner.

---



## Workstream A: real Google integrations



### A1: Mock services speak the real Google APIs

**Goal:** `mocks/main.py` answers the Gmail and Calendar endpoints Google answers, so the connectors have one
implementation (decision 1).

**Scope**

- Gmail (`https://gmail.googleapis.com/gmail/v1`):
  - `GET /users/{userId}/messages?q=&maxResults=` returns `{"messages": [{"id", "threadId"}], "resultSizeEstimate"}`.
  The real API returns **ids only**, which is the behaviour that matters: `search_emails` has to fetch each message
   afterwards. Support the `from:`, `subject:` and bare-word fragments of Gmail's `q` syntax and ignore the rest.
  - `GET /users/{userId}/messages/{id}?format=metadata|full` returns a message with `payload.headers`
  (`From`, `To`, `Subject`, `Date`) and, for `full`, a base64url `payload.body.data`.
  - `POST /users/{userId}/drafts` takes `{"message": {"raw": "<base64url RFC 5322>", "threadId"?}}` and returns
  `{"id", "message": {"id", "threadId"}}`.
  - `POST /users/{userId}/drafts/send` takes `{"id"}` and returns the sent message.
- Calendar (`https://www.googleapis.com/calendar/v3`):
  - `GET /calendars/{calendarId}/events?timeMin=&timeMax=&singleEvents=true&orderBy=startTime` returns
  `{"items": [{"id", "summary", "start": {"dateTime"}, "end": {"dateTime"}, "attendees": [{"email"}]}]}`.
  - `POST /freeBusy` takes `{"timeMin", "timeMax", "items": [{"id": "<email>"}]}` and returns
  `{"calendars": {"<email>": {"busy": [{"start", "end"}]}}}`.
  - `POST /calendars/{calendarId}/events?sendUpdates=` creates an event and echoes it back with an `id` and
  `htmlLink`.
- Keep `/_reset` and `/_stats`, and keep the seeded fixtures (the same accounts and renewal dates the demo SQL and
`evals/fixtures/` use). Existing eval cases must still pass unchanged.
- Bearer check: reject a request with no `Authorization: Bearer …` with Google's own error envelope
(`{"error": {"code": 401, "message": …}}`). The connectors must be exercised against a server that demands a token.
- Add an `/oauth/token` endpoint that answers the authorization-code and refresh-token grants with fake tokens and
a short `expires_in`. A4's refresh path is then testable without Google.

**Tests** (`tests/integration/test_mock_google_shapes.py`, and the existing gmail/calendar suites)

- A draft round-trips: `POST /drafts` with a base64url message, then `GET /messages/{id}` shows the decoded subject.
- `POST /freeBusy` returns busy blocks that match the events seeded for that attendee.
- A missing bearer token gives 401 in Google's envelope.

**Skipped:** pagination tokens, label management, recurring-event expansion. Add them when a tool needs them.

---



### A2: `memories` table, `oauth_expires_at`, repository

**Goal:** One Phase 7 migration for both schema changes (§14.3), plus the repository the memory tickets build on.

**Scope**

- New revision `phase7_memory_and_oauth`:
  - `memories` exactly as §14.3 specifies: `workspace_id`, nullable `user_id` (null = workspace scope), `scope`,
  `kind`, `content`, `confidence`, `source_run_id`, `embedding vector(768)`, `embedding_model`, `is_active`,
   `last_used_at`, `use_count`, timestamps. Index `(workspace_id, scope, is_active)`. Follow whatever vector index
   `document_chunks` uses so retrieval behaves the same way.
  - `ALTER TABLE connector_credentials ADD COLUMN oauth_expires_at timestamptz` — §14.3 lists it, the Phase 3
  migration never created it.
- `relay_core/db/models/memories.py` and `relay_core/db/repositories/memories.py`:
  - `create`, `list_visible_to(workspace_id, user_id, kind=None, include_inactive=False)`,
  `get`, `update`, `delete`.
  - `nearest(workspace_id, user_id, vector, limit, max_distance)` — user-scope rows for that user plus every
  workspace-scope row, active only, ordered by `cosine_distance`. Model it on
   `ToolDefinitionRepository.nearest_ids`.
  - `mark_used(ids)` bumps `use_count` and `last_used_at` in one statement.
  - Every method is workspace-scoped, like every other repository here.
- `ConnectorCredentialRepository.put` gains an `oauth_expires_at` argument, and a
`list_expiring_across_workspaces(before)` method for A4's sweep. Name the cross-tenant query loudly, the way
`ApprovalRepository.list_expired_across_workspaces` is named.

**Tests** (`tests/integration/test_memory_repository.py`)

- `nearest` returns a user's own memories and the workspace's, never another user's.
- An inactive memory is never returned.
- `mark_used` increments the counter and sets the timestamp.
- Cross-tenant: a second workspace's memories are invisible (add to `test_cross_tenant.py`).

---



### A3: Connector OAuth flow

**Goal:** An admin connects a real Google account to a `gmail` or `google_calendar` installation through the API
(§15.3, §17 install flow).

**Scope**

- `relay_core/connectors/oauth.py`: per-connector OAuth metadata — authorize URL, token URL, scopes. Gmail asks for
`gmail.readonly` and `gmail.compose`; Calendar asks for `calendar.readonly` and `calendar.events`. `google_oauth.py`
already builds a `Flow` for sign-in (ADR-0007), so reuse `google-auth-oauthlib` with different scopes rather than
hand-rolling the exchange.
- `GET /workspaces/{ws}/connectors/{id}/oauth/start` (admin only) returns `{"authorize_url"}` with
`access_type=offline`, `prompt=consent` (a refresh token only arrives with both) and PKCE `S256`.
  - The `state` parameter is a signed, 10-minute token carrying installation id, workspace id and user id. Reuse the
  existing JWT signing key (`relay_core/security/jwt.py`); do not invent a second signing scheme.
  - The PKCE verifier goes in Redis under a nonce from the state, TTL 10 minutes. It never travels in the URL.
- `GET /oauth/callback` (unauthenticated — Google calls it) validates the state signature and expiry, loads the
verifier, exchanges the code, and stores `{access_token, refresh_token, token_uri, scopes}` through the existing
KMS-encrypted `put`, with `oauth_expires_at`. It then runs the health check, syncs tools (`sync_installation`) and
redirects to `{app_base_url}/w/{slug}/connectors/{id}?connected=1`.
  - A failed exchange redirects with `?error=` and leaves the installation untouched.
  - The single registered redirect URI is `{api_base_url}/api/v1/oauth/callback`; everything else rides in `state`.
- Installing `gmail`/`google_calendar` with no `secrets` is allowed and leaves the installation `pending`. Until it
has a token, `validate_config` accepts it but the health check reports "not connected", so it never binds.
- Re-connecting an installation that already has a token replaces it (scope changes, revoked grant).

**Tests** (`tests/integration/test_connector_oauth.py`)

- `oauth/start` returns a URL carrying the right scopes, and a member (not admin) gets 403.
- A callback with a tampered or expired `state` is rejected, and no credential is written.
- A callback with a valid state stores the token, flips the installation to `active`/`healthy`, and creates
`tool_definitions` rows. Point `token_uri` at A1's `/oauth/token` so no network call leaves the test.
- A callback whose state names an installation in another workspace is rejected.

**Skipped:** incremental authorization and per-tool scope requests. Ask for both scopes at connect time; §10.3's
"send scope only if the admin enables send_draft" needs a UI that doesn't exist yet.

---



### A4: Token refresh, lazily and on a schedule

**Goal:** A run never calls Google with a stale token, and an unusable refresh token is visible before a user hits
it (§18.3 step 6).

**Scope**

- `relay_core/connectors/oauth.py::ensure_fresh_token(session, kms, installation) -> dict[str, str]`: returns the
secrets, refreshing first if `oauth_expires_at` is within 60 seconds. One helper, called from both places below, so
there is only one refresh implementation.
- Lazy path: whatever loads secrets for a call (`ToolRegistry`/`ToolExecutor`) goes through it. A worker that wakes
after a long idle refreshes on the spot.
- Scheduled path: `relay_worker/tasks/connectors.py::refresh_oauth_tokens`, beat every 5 minutes, over
`list_expiring_across_workspaces(now + 15 min)`. Per-installation error isolation, like the Phase 6 sync sweep.
- A refresh that fails with `invalid_grant` (revoked or expired consent) sets the installation `degraded` with a
health message naming reconnection as the fix. A network failure is left alone for the next tick — a blip must not
mark a working installation degraded.
- Never log tokens. The refresh writes through the same KMS envelope as A3.

**Tests** (`tests/integration/test_oauth_refresh.py`)

- An installation expiring in 30 seconds is refreshed before the call, and the new token is what the connector sends.
- The beat task refreshes an installation expiring in 10 minutes and leaves one expiring in an hour alone.
- `invalid_grant` marks the installation `degraded`; a connection error does not.
- Concurrent callers don't write two different tokens (refresh under a row lock, or accept the last write and assert
the stored token still works).

---



### B1: `gmail` against the real Gmail API

**Goal:** `gmail` speaks Gmail, whether `base_url` points at Google or at A1's mock.

**Scope**

- `search_emails`: `GET /users/me/messages` for ids, then fetch each with `format=metadata` and
`metadataHeaders=From&Subject&Date`. Cap the fan-out (`max_results` ≤ 10, fetched concurrently) and say so in the
tool description, because this is now N+1 requests rather than one.
- `get_email`: `format=full`, walk `payload.parts` for the first `text/plain` part, base64url-decode, fall back to
stripping `text/html` with the helper `web_search` already uses.
- `create_draft`: build the message with stdlib `email.message.EmailMessage` (`To`, `Cc`, `Subject`, body), then
`base64.urlsafe_b64encode`. Keep `thread_id` support by setting `threadId` on the message.
- `send_draft`: `POST /users/me/drafts/send` with `{"id"}`.
- Auth: `Authorization: Bearer <access_token>` from secrets. The recipient guard (`blocked_recipients`) stays exactly
where it is — it is enforced before the request is built, not after.
- Gmail has no idempotency header, so nothing is forwarded. Relay's own replay guard (ADR-0011) is what protects a
crash between send and checkpoint, and the docstring should say so rather than implying the remote dedupes.
- `config.sender_address` becomes optional metadata only; the real API uses `me`.

**Tests** (`tests/integration/test_gmail_calendar_connectors.py`, extended)

- A draft created through the connector comes back from `GET /messages/{id}` with the same subject, body and
recipients, decoded from `raw`.
- `search_emails` issues one list call and one fetch per hit, and returns summaries without bodies.
- A recipient outside `email_domain_allow` fails the call before any HTTP request is made.
- A 401 from Google surfaces as a tool error naming reconnection, not as a stack trace.

---



### B2: `google_calendar` against the real Calendar API

**Goal:** Same swap for Calendar.

**Scope**

- `list_events`: `GET /calendars/{calendar_id}/events` with `singleEvents=true&orderBy=startTime` and RFC 3339
`timeMin`/`timeMax`.
- `find_free_slots`: `POST /freeBusy` for the attendees, then keep the existing slot arithmetic — it moves from
diffing events to diffing `busy` blocks, which is the same code over a simpler input. This stays a tool rather than
something the model works out (see the connector's docstring).
- `create_event`: `POST /calendars/{calendar_id}/events` with `start`/`end` as `{"dateTime", "timeZone"}` and
attendees as `[{"email"}]`.
  - `sendUpdates` comes from config and **defaults to `none`**. A demo run must not mail invitations to real people
  as a side effect; an admin who wants invites sets it explicitly.
- Time zones: the connector sends an explicit `timeZone` (config, default `UTC`) rather than relying on the
calendar's default, so a free-slot answer and the event it creates agree.

**Tests** (`tests/integration/test_gmail_calendar_connectors.py`, extended)

- `find_free_slots` skips a window covered by a `busy` block from `freeBusy`.
- A created event round-trips through `list_events` with the same start, end and attendees.
- `sendUpdates=none` is on the request unless config says otherwise.

---



## Workstream C: closing the graph



### C1: `replan` node

**Goal:** A failed or newly-informed step revises the rest of the plan instead of ending the run (§8.2, §8.5).

**Scope**

- `relay_core/agent/nodes/replan.py`, `PLANNER` profile: given the objective, the plan with each step's status and
`result_summary`, the failure reason, and `available_capabilities`, return a revised `Plan`.
  - Completed steps are preserved verbatim, including their results. Only `pending`/`failed` steps may be replaced.
  Enforce that in code after parsing, not in the prompt alone.
  - Emit `plan.updated` (new constant in `relay_core/events/types.py`, §16.3).
- State: `replans_used: int` on `AgentState`, bounded at 2. At the bound the run stops replanning and goes to
`synthesize` with what it has — a truthful partial answer beats a loop.
- Wiring: `validate_step`'s `"replan"` verdict routes to `replan` (today it is treated as `"fail"`), and
`replan` edges to `check_capabilities`, so a revised plan that needs something the workspace doesn't have lands on
the existing missing-capability card.
- A parse failure or an empty revision is a failure, not a retry: mark the step `failed` and continue, exactly as
today.

**Tests** (`tests/integration/test_replan.py`, scripted model)

- A step whose validator returns `replan` produces a new plan, and the run finishes on the revised steps.
- Completed steps and their results survive replanning.
- The third replan doesn't happen; the run synthesizes instead.
- A revision requiring a missing capability routes to `ask_missing`.

---



### C2: `validate_final` groundedness check

**Goal:** Every number and claim in the final answer is traceable to a tool output or a cited source (§8.5).

**Scope**

- `relay_core/agent/nodes/validate_final.py`, `VALIDATOR` profile. Input: the draft answer plus the step results and
`sources` it is supposed to rest on. Output:

  ```python
  class FinalVerdict(BaseModel):
      status: Literal["pass", "revise"]
      unsupported_claims: list[str] = []
      reason: str
  ```

- `synthesize` stops publishing `token` events on its first pass and returns the draft in state (decision 5). It
streams on the pass that reaches `finalize`. One extra validator call per task run is the cost; an answer the user
watched appear and then silently change is what it buys off.
- `final_revisions: int` on `AgentState`, bounded at 1 (§8.2's "revise (max 1)"). The revision pass gets
`unsupported_claims` in its prompt and is told to cut or qualify them, never to invent support.
- A second `revise` verdict, or an unparseable verdict, goes to `finalize` anyway. The check degrades to a no-op, it
never blocks an answer.
- Skip the check entirely on the `direct_answer` and `blocked` paths — there are no tool outputs to be grounded in.

**Tests** (`tests/integration/test_validate_final.py`, scripted model)

- A draft containing a number no step produced is revised once, and the revised text is what is persisted and
streamed.
- A grounded draft passes with exactly one validator call and no revision.
- Two `revise` verdicts in a row still finalize.
- No `token` event carries text from the rejected draft.

---



## Workstream D: memory



### D1: Memory extraction after a run

**Goal:** Durable facts and preferences are extracted from completed runs (§12.2).

**Scope**

- `relay_core/memory/extract.py` with §12.2's `ExtractedMemory` model (`content`, `kind`, `scope`, `confidence`,
`supersedes_id`), `LIGHT` profile, one call per run over the user message, the final answer and the step summaries.
- `relay_worker/tasks/memory.py::extract_memories(run_id)`, queue `memory` (already routed in
`relay_worker/app.py`), enqueued from `finalize` — §8.5 gives `finalize` that job. Enqueue after the run's
transaction commits, and never let a failure there fail the run.
- Rules enforced in code, not only in the prompt (the pattern from the Phase 6 tagger):
  - Drop anything below `confidence >= 0.75`.
  - Drop content that looks like a secret: anything matching an API-key/token/password shape, or an argument that
  came from `secrets`. When in doubt, drop it.
  - Deduplicate by embedding: cosine similarity `> 0.92` against an existing memory updates that row
  (content, confidence, `source_run_id`) instead of inserting a near-duplicate.
  - `scope: "workspace"` is only accepted from an owner or admin's run; anyone else's workspace-scope proposal is
  stored as user scope.
  - Skip the whole task when `workspace_policies.memory_enabled` is false, or the run failed or was blocked.
- Embed with `gateway.embed(..., task="RETRIEVAL_DOCUMENT")` and store `embedding_model`, like document chunks.

**Tests** (`tests/integration/test_memory_extraction.py`, scripted model)

- A stated preference becomes one `preference` memory with the source run recorded.
- A 0.5-confidence extraction is dropped.
- A near-duplicate updates the existing row instead of adding one.
- Something shaped like an API key is never stored.
- `memory_enabled=false` stores nothing.

---



### D2: Memory retrieval in `load_context`

**Goal:** What the agent remembered actually changes what it does (§12.3).

**Scope**

- `load_context` embeds the user message (`RETRIEVAL_QUERY`), fetches the top 5 above the similarity threshold from
`MemoryRepository.nearest`, and calls `mark_used`.
- New `memories: list[str]` on `AgentState` (rendered strings, not rows — state is checkpointed, and a row shape
would be a compatibility surface for no benefit).
- Injected into the `plan`, `execute_step`, `direct_answer` and `synthesize` prompts under a heading that says these
are remembered preferences about this user and workspace, not instructions from the current message, and that a
memory never authorizes an action — approvals still apply.
- An embedding failure is non-fatal: no memories, run continues. Same posture as Phase 6's tool retrieval.
- Respect `memory_enabled` here too.

**Tests** (`tests/integration/test_memory_retrieval.py`)

- A memory about tone reaches the prompt for a related message and is absent for an unrelated one.
- `use_count` is incremented when a memory is used.
- An embedding failure leaves the run working.
- Another user's user-scope memory never appears.

---



### D3: Memories API

**Goal:** `GET/PATCH/DELETE /workspaces/{ws}/memories[/{id}]` (§15.5), so a user can see and correct what was stored.

**Scope**

- `relay_api/routers/memories.py`:
  - `GET` lists the caller's own memories plus workspace-scope ones, with `kind`, `scope` and `q` filters. Never
  returns embeddings.
  - `PATCH` edits `content`, `kind`, `is_active`. Editing the content re-embeds the row.
  - `DELETE` removes it for good.
- Permissions: a member manages their own memories; only an owner or admin may edit or delete a workspace-scope
memory. A non-member gets 404, matching every other router here.

**Tests** (`tests/integration/test_memories_router.py`)

- List, edit, deactivate, delete round-trip.
- A member cannot edit a workspace-scope memory.
- Editing the content changes the embedding (the row's retrieval behaviour changes).
- Cross-tenant isolation (add to `test_cross_tenant.py`).

---



### D4: Conversation summarization

**Goal:** A long conversation keeps its earlier context without sending every message (§12.1).

**Scope**

- `relay_core/memory/summarize.py`, `LIGHT` profile: summarize messages older than the last 8 into
`conversations.summary`, and record `summary_upto_message_id` — both columns already exist from Phase 2.
- Triggered from `finalize` when the conversation holds more than 20 messages past the last summary point. A rough
character-count stand-in for §12.1's 30k-token threshold is fine; say so in the docstring.
- Re-summarizing folds the previous summary into the new one rather than starting over.
- `load_context` sets `history_summary` from the column, and the prompts that already accept it start receiving
something other than `None`.

**Tests** (`tests/integration/test_summarization.py`, scripted model)

- A 25-message conversation gets a summary and a `summary_upto_message_id` at the right message.
- A short conversation gets none.
- The next run's context carries the summary plus the last 8 messages.

---



## Workstream E: resilience



### E1: Circuit breaker and installation health monitoring

**Goal:** A connector that is failing stops being chosen, and comes back on its own (§19.1).

**Scope**

- `relay_core/connectors/breaker.py`, Redis-backed, keyed by installation id: 5 failures inside 60 seconds opens the
breaker for 120 seconds. Record from `ToolExecutor` — a connector error or timeout counts, a tool that returned a
clean business error does not.
- An open breaker takes the installation out of binding, so `ToolDefinitionRepository.list_bindable` drops it and the
capability falls to the next installation by priority (decision 7). No planner change.
- Opening sets `health = "degraded"` with a message; the next successful call or the health sweep clears it.
- The Phase 6 six-hourly sweep is the health monitor; give it a second, shorter beat for installations currently
`degraded` or `down` (every 10 minutes) so recovery is noticed in minutes rather than hours.
- Redis being unavailable must not break tool calls: a breaker that can't be read is closed.

**Tests** (`tests/integration/test_circuit_breaker.py`)

- Five failures open the breaker and the sixth call never reaches the connector.
- With two installations providing one capability, the open one is skipped and the other binds.
- The breaker closes after its window and the installation binds again.
- A Redis outage leaves calls working.

---



## Workstream F: proof and docs



### F1: Evals: `planning` suite, groundedness and memory cases

**Goal:** The new graph nodes and memory are measured, not just tested (§21.1).

**Scope**

- New `evals/suites/planning/` (§21.1's gate: capability recall ≥ 0.9): cases where the first plan cannot work — a step
whose tool fails, and a step whose result makes a later step pointless — scored on the run finishing with a revised
plan and a truthful answer.
- A groundedness case in `task_success`: the answer must mention only numbers the seeded data contains
(`must_mention` / `must_not_mention` already support this).
- A memory case: two runs in one conversation-less sequence, where the first states a preference and the second is
expected to honour it. Needs a harness option to run two cases in order against the same workspace — keep it as a
`depends_on_case` key rather than a new runner.
- Register `planning` in `relay_eval/cli.py`'s suite list and gates.

**Tests:** the suites themselves, plus one scripted-model integration test proving the replan case fails when C1's
node is removed. A gate that cannot go red is not a gate (the Phase 5 lesson).

---



### F2: ADR-0013, status note, README, `.env.example`

**Goal:** The phase is explainable without reading the diff.

**Scope**

- `docs/adr/0013-real-oauth-memory-and-replanning.md`: decisions 1–7 above, with the alternatives that were
rejected — a mock-mode branch in each connector, a second signing scheme for OAuth state, `approval_gate --> replan`
as drawn in §8.2, and streaming the unvalidated draft.
- `docs/phase-7-status.md` in the shape of [phase-6-status.md](phase-6-status.md): tickets done, test counts,
decisions not in the design doc, what is unverified, known rough edges.
- README Phase 7 paragraph. `.env.example`: note that `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` now also serve connector
OAuth, and document the redirect URI that must be registered in the Google Cloud console.
- `docs/system-design.md` is left as written, as in ADR-0006; the ADR is the record of the HubSpot-shaped hole in
§28's "done when".

---



## Deferred to the frontend pass

- The connector detail page's **Connect with Google** button and the `?connected=1` / `?error=` states A3 redirects to.
- The **memory page** (`/w/[slug]/memory`, §24): list, edit, deactivate, delete, with the workspace/user scope split.
- Replan and groundedness in the run timeline (`plan.updated`, revision notices).

The backend routes above are the whole contract those pages need.

## Not in this phase

- Injection defenses and the injection suite, budgets, rate limits, metrics, the retention job (Phase 8).
- Incremental OAuth scopes, Google Workspace domain-wide delegation, publishing the OAuth app past test mode.
- HubSpot, permanently ([adr/0006](adr/0006-hubspot-dropped-from-scope.md)).
