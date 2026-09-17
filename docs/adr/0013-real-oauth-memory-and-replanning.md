# ADR-0013: Real Google OAuth, memory, replanning and groundedness

## Status

Accepted. Records the seven decisions Phase 7 (docs/system-design.md section 28) was built on.
Extends [ADR-0011](0011-approvals-interrupt-resume-and-enforcement-in-code.md) (decision 4 below
settles the `approval_gate --> replan` edge it left open) and
[ADR-0012](0012-tool-definitions-and-byo-tools.md) (decision 7 reuses its binding rules). The
HubSpot half of section 28's "done when" remains out of scope per
[ADR-0006](0006-hubspot-dropped-from-scope.md).

## Context

Phase 7's "done when" is three separate things: the demo runs against **real Gmail drafts and
real Google Calendar events** through a test-mode OAuth app, the agent **remembers a stated
preference across conversations**, and a **failed step is replanned** instead of being reported
as a dead end.

Each of those touches code that already worked against something simpler. Gmail and Calendar
spoke a mock service with a shape Relay invented. Memory existed only as a column nobody wrote
and a table that did not exist. The graph had a `validate_step` verdict named `replan` that was
treated as `fail`, and a `synthesize` that streamed straight to the user with nothing between it
and the screen. The decisions below are the ones where the obvious route was not the one taken.

## Decision

### 1. One code path per connector, not a "mock mode" branch

`mocks/main.py` was rewritten to answer the Gmail and Calendar REST endpoints Google answers —
ids-only message lists, base64url RFC 5322 drafts, `freeBusy`, Google's own 401 envelope — and
the connectors always speak Google. Pointing an installation at the mock is a `config.base_url`
difference, exactly as it already was for `web_search` against Tavily.

A connector carrying `if mock:` branches would drift, and the branch every eval and every test
exercised would be the one that is never the real one.

### 2. Tool names, arguments and risks did not change

Only the HTTP underneath moved. Every Phase 5 and Phase 6 approval, eval case and
`tool_definitions` row keeps working, and `schema_hash` stays stable, so nothing was sent to
review by an upgrade. `config.sender_address` became optional metadata; the real API uses `me`.

### 3. OAuth tokens live in the existing encrypted secrets blob; only the expiry gets a column

`connector_credentials.oauth_expires_at` (section 14.3) is the one field the refresh sweep needs
in the clear, because it has to find expiring installations without decrypting every credential
in the database. Everything else — access token, refresh token, token URI, scopes — stays inside
the KMS envelope that already existed.

The OAuth `state` parameter is signed with the existing JWT key
(`relay_core/security/jwt.py`). The PKCE verifier goes to Redis under a nonce carried in that
state and never travels in the URL.

### 4. A rejected approval comes back to the model as a tool result, not straight to `replan`

Section 8.2 draws an `approval_gate --> replan` edge. It is deliberately not built.

Gemini requires a function response for every function call in a model turn, so a rejection has
to be answered as a function response for the turn to be valid at all (ADR-0011 decision 5).
Beyond that, the step's own validator is a better judge than the gate of whether the plan is now
unworkable: a declined draft often means "write it differently", not "the plan is dead".
`validate_step` reaching a `replan` verdict is the single entry to replanning.

Replanning is bounded at two revisions. At the bound the run synthesizes what it has — a
truthful partial answer beats a loop that spends a budget rediscovering one dead end. Completed
steps are carried over verbatim **in code**, not by asking the model nicely, so a revision can
never erase a result the answer rests on.

### 5. `synthesize` stops streaming its first draft when a validator follows it

`validate_final` can send a draft back for one revision. A groundedness check that can demand a
revision is worthless if the unvalidated text is already on the user's screen, so `synthesize`
collects its deltas into `draft_chunks` and `validate_final` publishes them — in the pieces they
arrived in — only once the draft is the one that will be finalized.

The cost is one validator call and a slightly later first token. What it buys off is a user
watching an answer appear and then silently change. The check degrades to a no-op rather than
blocking: a second `revise`, or an unparseable verdict, finalizes the draft anyway.
`direct_answer` and the guard's `blocked` path skip it entirely — there are no tool outputs for
them to be grounded in, and they still stream as they always did.

### 6. Memory is off unless the workspace says otherwise

`workspace_policies.memory_enabled` already existed and defaults to true, but extraction and
retrieval both check it, and `finalize` checks it before enqueueing, so a workspace can turn the
feature off without a deploy. Extraction re-checks inside the task, because the policy can change
between the enqueue and the worker picking the job up.

What the extractor proposes is filtered **in code**, the same split as the Phase 6 capability
tagger: confidence below 0.75 is dropped, anything shaped like a credential is dropped, a
near-duplicate (cosine similarity above 0.92) updates the existing row instead of inserting, and
a workspace-scope proposal from anyone below admin is stored as that person's own memory. A
prompt saying "never store secrets" is a request; those filters are the guarantee.

Retrieved memories reach the prompt under a heading that says they are background preferences
from earlier conversations, never instructions from the current message, and that a memory does
not authorize an action — approvals still apply.

### 7. A breaker-open installation is treated exactly like an unhealthy one

Five connector failures inside sixty seconds open a Redis-backed breaker for two minutes. The
installation stops binding, so the resolver reports its capabilities as provided by the next
installation in priority order and the planner needs no change at all.

Only *connector* failures count. A `ToolResult(ok=False)` the connector returned is a working
connector answering a question, and argument validation never reached the connector. A breaker
that counted those would open on a healthy installation being asked the wrong questions.

Redis being unavailable must not break tool calls: a breaker that cannot be read is closed.

## Consequences

- **The demo needs a Google Cloud OAuth client.** `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` now serve
  both sign-in (ADR-0007) and connector OAuth, and `{API_BASE_URL}/api/v1/oauth/callback` must be
  registered as an authorized redirect URI. A `gmail` or `google_calendar` installation with no
  token installs fine, stays `pending`, reports "not connected" and never binds.
- **Every task run costs one more LLM call** (`validate_final`), and every completed run enqueues
  one more (`extract_memories`, on the `memory` queue). A conversation past twenty messages costs
  one more again for summarization, awaited inline in `finalize` because it fires on roughly one
  turn in twenty and a queue hop would be more machinery than the work it defers.
- **`AgentState` gained five checkpointed fields** — `memories`, `replan_reason`, `replans_used`,
  `draft_chunks`, `unsupported_claims`, `final_revisions`. A run parked on an approval when this
  shipped resumes into a process reading state written by the older shape; all of them default,
  so that resume works.
- **Memory extraction is enqueued after the run's transaction commits**, through SQLAlchemy's
  `after_commit` event rather than an outbox table. A task enqueued inside the transaction could
  be picked up before `agent_runs` says `completed`, and extraction refuses to run on anything
  that is not.
- **The breaker filter runs over rows, not inside SQL.** Breaker state lives in Redis and
  `list_bindable` is a Postgres query, so one `MGET` over the candidate installations is cheaper
  than teaching the repository about Redis. `capabilities.resolver.drop_tripped` is shared by the
  resolver and the registry so "what is available" and "what binds" can never disagree.
- **A second, ten-minute beat** re-checks only installations that are already `degraded` or
  `down`. Six hours is right for noticing that a working connector changed its tools and far too
  slow for noticing that a broken one came back.
- **`ConnectorInstallationRepository.list_unhealthy_across_workspaces` and
  `ConnectorCredentialRepository.list_expiring_across_workspaces`** are two more deliberate
  cross-tenant queries, named loudly for the same reason as ADR-0012's.

## Alternatives considered

- **A mock-mode branch in each connector.** Rejected: see decision 1. The tested path would be
  the one that never runs in production.
- **A second signing scheme for the OAuth `state`.** Rejected: the JWT key already exists, is
  already rotated, and is already the thing whose compromise would matter. A second scheme is a
  second thing to get wrong.
- **`approval_gate --> replan`, as section 8.2 draws it.** Rejected: see decision 4. The
  function-response requirement makes it invalid, and the validator is the better judge.
- **Streaming the unvalidated draft and correcting it afterwards.** Rejected: a correction the
  user watches happen is worse than an answer that took a second longer. Streaming is kept for
  `direct_answer`, where nothing follows it.
- **Storing retrieved memories on `AgentState` as rows.** Rejected: state is checkpointed, so a
  row shape would be a compatibility surface for no benefit. Nothing downstream needs a memory's
  id, only its text.
- **Enforcing the extractor's rules in the prompt alone.** Rejected, for the reason ADR-0012
  gives about the tagger: the model proposes, code decides.
- **A `capability_bindings`-style per-capability breaker.** Not needed: the breaker is
  per-installation, and an installation that is failing is failing for every capability it
  provides.
