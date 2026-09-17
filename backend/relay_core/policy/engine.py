"""The approval decision (docs/system-design.md section 13.1) and the recipient guard that
goes with it (section 10.3).

`needs_approval` takes primitives — a risk, a tool name, the call's arguments, the parsed rules
and the caller's role — rather than the `ToolSpec`/`BoundTool`/`User` objects the design sketch
passes around. Keeping the signature free of registry and ORM types is what lets this be a pure
function: the whole of section 13.1's matrix is unit-testable without a database, a workspace or
a connector, which matters because `approval_compliance` is a blocking CI gate (section 21.1) and
a gate is only worth as much as the tests underneath it.

**This is the enforcement point, and it is code, not prompt** (section 18.1's "Approval
enforcement is in code, not in the prompt — the model can't skip it"). Nothing here consults the
model, and no prompt text can reach it.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from relay_core.connectors.base import Risk

Rule = Literal["always", "never", "over_threshold"]

_ELEVATED_ROLES = frozenset({"owner", "admin"})


class ItemCountThreshold(BaseModel):
    """"Approve automatically unless this argument carries more than `max_items` entries" —
    section 13.4's "> 5 recipients" example. A missing or non-list argument counts as 0."""

    arg: str
    max_items: int = Field(ge=0)


class ApprovalOverride(BaseModel):
    tool: str
    rule: Rule
    threshold: ItemCountThreshold | None = None


class ApprovalRules(BaseModel):
    default_write: Rule = "always"
    overrides: list[ApprovalOverride] = Field(default_factory=list)

    def for_tool(self, tool_name: str) -> tuple[Rule, ItemCountThreshold | None]:
        for override in self.overrides:
            if override.tool == tool_name:
                return override.rule, override.threshold
        return self.default_write, None


def parse_approval_rules(raw: dict[str, Any]) -> ApprovalRules:
    """Hand-edited or partially-migrated policy JSON falls back to the safe default rather than
    failing the run: an unparseable rule set must not become an accidental `never`."""
    try:
        return ApprovalRules.model_validate(raw)
    except ValidationError:
        return ApprovalRules()


def needs_approval(
    *,
    risk: Risk,
    tool_name: str,
    args: dict[str, Any],
    rules: ApprovalRules,
    user_role: str,
) -> bool:
    if risk is Risk.DESTRUCTIVE:
        # Section 13.1: destructive calls are never waivable, whatever the rules or the role say.
        return True
    if risk is not Risk.WRITE:
        return False

    rule, threshold = rules.for_tool(tool_name)
    if rule == "never":
        return user_role not in _ELEVATED_ROLES
    if rule == "over_threshold" and threshold is not None:
        return _item_count(args, threshold.arg) > threshold.max_items
    return True


def _item_count(args: dict[str, Any], arg: str) -> int:
    value = args.get(arg)
    return len(value) if isinstance(value, list) else 0


def blocked_recipients(recipients: list[str], allowed_domains: list[str]) -> list[str]:
    """Which of `recipients` the workspace's `email_domain_allow` forbids (section 10.3).

    An empty allow-list means "no domain restriction configured", not "allow nothing" — that is
    the shipped default (`workspace_policies.email_domain_allow` defaults to `'{}'`), and a
    workspace that has never opened the policy page should still be able to draft email.
    """
    if not allowed_domains:
        return []
    allowed = {d.lower().lstrip("@") for d in allowed_domains}
    return [r for r in recipients if _domain_of(r) not in allowed]


def _domain_of(address: str) -> str:
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()
