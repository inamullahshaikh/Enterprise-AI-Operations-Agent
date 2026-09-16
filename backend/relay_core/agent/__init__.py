"""LangGraph graph, state, and node implementations (docs/system-design.md section 8).
Phase 2 wires the subgraph that needs no connectors: `load_context` through either
`direct_answer`/`finalize` or `plan` -> `check_capabilities` -> `ask_missing`. Phase 3 adds the
step-execution loop: `check_capabilities` -> `next_step` -> `execute_step` -> `validate_step` ->
(retry `execute_step` or move on via `next_step`) -> `synthesize` -> `finalize`. `approval_gate`
and `replan` (Phase 5/7) are still unreachable — Phase 3's built-in tools are all `read` risk,
and a step needing replanning is honestly marked `failed` instead (see `validate_step`'s
docstring).
"""
