# ADR-0001: Plan-and-execute graph instead of a single ReAct loop

## Status

Proposed

## Context

A single ReAct loop (think → call a tool → observe → repeat) is simple to build and works
for short tasks, but for multi-step operational work it makes several things hard to do well:
showing the user a plan before acting, checking whether required capabilities are even
available before spending tool calls, budgeting per step rather than per whole run, and
inserting a validation checkpoint after each step so failures are caught before they compound.

## Decision

Relay uses a bounded **plan → check capabilities → execute (bounded ReAct per step) →
validate → synthesize** LangGraph graph, with a fast `direct_answer` path for messages that
need no tools at all. See docs/system-design.md section 8 for the full graph, state shape, and
node responsibilities.

## Consequences

- More moving parts and prompts to maintain than a single loop.
- The planner's output becomes a first-class, inspectable artifact (shown in the UI, stored,
  evaluated), which is central to the product's "show your work" and capability-detection goals.
- Replanning and per-step validation are natural extension points instead of bolted-on logic.

## Alternatives considered

- Single ReAct loop over all available tools: rejected — no visible plan, no place to check
  capabilities up front, budgets only enforceable at the whole-run level.
- Fully deterministic workflow (no planning LLM call, hardcoded step graph per task type):
  rejected — doesn't generalize across arbitrary user requests and arbitrary connector sets.

Section 21.6 of the design doc records an experiment comparing a single ReAct loop against
this graph on success rate, cost, and latency; fill in the real numbers once Phase 8 evals run.
