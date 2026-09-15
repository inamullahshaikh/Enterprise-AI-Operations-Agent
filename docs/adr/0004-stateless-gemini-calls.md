# ADR-0004: Stateless Gemini calls with Relay-owned checkpoints

## Status

Proposed

## Context

Gemini function-calling turns carry "thought signatures" on function-call parts that must be
sent back exactly as received to preserve reasoning continuity. Google's Interactions API can
manage conversation state and these signatures server-side, which would simplify the client.
Relay, however, needs every run to be durably resumable after a worker crash or an
`interrupt()` for approval, and needs a full, provider-independent audit trail of every turn.

## Decision

Relay's LLM gateway makes stateless calls to the Gemini API and manages all conversation state
itself: LangGraph's Postgres checkpointer persists the exact SDK `Content` objects returned
from each call (including thought signatures) rather than reconstructing turns from plain
text (docs/system-design.md section 9.1). Automatic function calling is disabled so every tool
call passes through Relay's own approval, logging, and budget enforcement.

## Consequences

- Runs survive worker restarts and resume from the exact point they paused, which the
  approval flow (section 13) depends on.
- Full auditability: every model turn, tool call, and decision is a row in Postgres, not
  opaque server-side state on Google's side.
- More bookkeeping than a managed-state API — the executor must never rebuild model turns
  from summarized text, only replay the stored `Content` objects.
- The design stays portable to a future non-Gemini provider behind the same gateway interface.

## Alternatives considered

- Interactions API (server-managed state and signatures): simpler client code, but ties
  durability and auditability to provider-side state that Relay doesn't control. Worth
  re-evaluating if it matures, but documented here as a deliberate trade-off, not an oversight.
