"""The capability taxonomy (docs/system-design.md section 7.1) — kept small and
stable per the design doc's own guidance. This is the vocabulary the planner
plans against; whether any of these are actually resolvable in a given
workspace is a Phase 3+ concern (`relay_core.capabilities` docstring).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityDefinition:
    key: str
    meaning: str


CAPABILITY_TAXONOMY: list[CapabilityDefinition] = [
    CapabilityDefinition("knowledge.search", "Search internal documents"),
    CapabilityDefinition("customer.read", "Read customer/account records"),
    CapabilityDefinition("subscription.read", "Read subscription/billing records"),
    CapabilityDefinition("usage.read", "Read product usage metrics"),
    CapabilityDefinition("deal.read", "Read sales pipeline"),
    CapabilityDefinition("crm.note.write", "Add notes/tasks to CRM"),
    CapabilityDefinition("sql.query", "Run read-only SQL"),
    CapabilityDefinition("email.read", "Read/search email"),
    CapabilityDefinition("email.draft", "Create email drafts"),
    CapabilityDefinition("email.send", "Send email"),
    CapabilityDefinition("calendar.read", "Read events/free-busy"),
    CapabilityDefinition("calendar.write", "Create events"),
    CapabilityDefinition("web.search", "Search the public web"),
    CapabilityDefinition("web.fetch", "Fetch a URL"),
    CapabilityDefinition("code.execute", "Run Python for analysis/charts"),
    CapabilityDefinition("file.read", "Read user-uploaded files"),
]


def render_catalog() -> str:
    """`{capability} — {meaning}` lines for the planner system prompt."""
    return "\n".join(f"{c.key} — {c.meaning}" for c in CAPABILITY_TAXONOMY)
