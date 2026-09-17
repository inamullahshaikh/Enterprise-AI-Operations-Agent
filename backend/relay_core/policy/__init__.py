"""Workspace governance (docs/system-design.md sections 13.1, 18.1).

Phase 5 implements the approval half: `needs_approval` decides, in code, whether a write call
has to stop for a human, and `blocked_recipients` enforces the email domain allow-list. The
budget half of `workspace_policies` (section 19's per-run and per-month limits) is stored but not
yet enforced — that lands with Phase 8's hardening, against the same `run_budget` column.
"""

from relay_core.policy.engine import (
    ApprovalOverride,
    ApprovalRules,
    ItemCountThreshold,
    Rule,
    blocked_recipients,
    needs_approval,
    parse_approval_rules,
)

__all__ = [
    "ApprovalOverride",
    "ApprovalRules",
    "ItemCountThreshold",
    "Rule",
    "blocked_recipients",
    "needs_approval",
    "parse_approval_rules",
]
