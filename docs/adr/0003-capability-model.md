# ADR-0003: Plan against capabilities, not tools

## Status

Proposed

## Context

Real companies connect different tools for the same job — one uses HubSpot for customer
data, another keeps it in Postgres, a third only has a CSV export. If the planner and executor
reason directly about concrete tool names, every prompt and every plan becomes coupled to one
workspace's specific connector set, and "missing data" can only be discovered by a tool call
failing at execution time.

## Decision

Introduce an abstract **capability** layer (`customer.read`, `usage.read`, `email.draft`, …,
see docs/system-design.md section 7.1). The planner requests capabilities, not tools. A
resolver maps each capability to the best available installation in the workspace (by
priority and health), with a documented fallback table (e.g., `usage.read` falling back to an
uploaded CSV) and a `check_capabilities` graph node that runs **before** any tool call, so
missing data is reported honestly instead of discovered mid-execution or hallucinated.

## Consequences

- Every built-in, MCP, and OpenAPI tool must declare (or be tagged with) the capabilities it
  provides, adding a classification step for third-party tools (section 7.4).
- The same plan and the same prompts work unchanged across the full connector-configuration
  matrix (section 21.2), which is what makes the `config_matrix` eval suite meaningful.
- The capability taxonomy is a piece of product surface area that has to be kept small and
  stable (section 7.1) — an ever-growing list of overly specific capabilities would defeat
  the purpose.

## Alternatives considered

- Let the planner reason directly over the concrete tool list: rejected — couples every plan
  to one workspace's connectors and gives no clean point to detect gaps before execution.
