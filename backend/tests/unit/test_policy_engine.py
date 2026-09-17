"""docs/system-design.md section 13.1's approval matrix. `approval_compliance` is a blocking
CI gate (section 21.1) and these are the tests underneath it, so every branch of
`needs_approval` is covered here — especially the ones that return False."""

import pytest

from relay_core.connectors.base import Risk
from relay_core.policy import (
    ApprovalOverride,
    ApprovalRules,
    ItemCountThreshold,
    blocked_recipients,
    needs_approval,
    parse_approval_rules,
)

ALL_ROLES = ["owner", "admin", "member", "viewer"]


def _decide(risk: Risk, rules: ApprovalRules, role: str, args: dict | None = None) -> bool:
    return needs_approval(
        risk=risk, tool_name="gmail__send_draft", args=args or {}, rules=rules, user_role=role
    )


@pytest.mark.parametrize("role", ALL_ROLES)
def test_read_calls_never_need_approval(role: str) -> None:
    assert _decide(Risk.READ, ApprovalRules(), role) is False


@pytest.mark.parametrize("role", ALL_ROLES)
def test_destructive_always_needs_approval_regardless_of_rule_or_role(role: str) -> None:
    """Section 13.1: `destructive` short-circuits before the rules are consulted at all, so
    even an owner with an explicit `never` override cannot waive it."""
    rules = ApprovalRules(
        default_write="never",
        overrides=[ApprovalOverride(tool="gmail__send_draft", rule="never")],
    )
    assert _decide(Risk.DESTRUCTIVE, rules, role) is True


@pytest.mark.parametrize("role", ALL_ROLES)
def test_write_defaults_to_always(role: str) -> None:
    assert _decide(Risk.WRITE, ApprovalRules(), role) is True


@pytest.mark.parametrize(("role", "expected"), [("owner", False), ("admin", False)])
def test_never_waives_approval_for_elevated_roles(role: str, expected: bool) -> None:
    assert _decide(Risk.WRITE, ApprovalRules(default_write="never"), role) is expected


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_never_does_not_waive_approval_for_ordinary_roles(role: str) -> None:
    assert _decide(Risk.WRITE, ApprovalRules(default_write="never"), role) is True


@pytest.mark.parametrize(
    ("recipients", "expected"),
    [([], False), (["a@x.com"], False), (["a@x.com"] * 5, False), (["a@x.com"] * 6, True)],
)
def test_over_threshold_compares_item_count(recipients: list[str], expected: bool) -> None:
    rules = ApprovalRules(
        default_write="over_threshold",
        overrides=[
            ApprovalOverride(
                tool="gmail__send_draft",
                rule="over_threshold",
                threshold=ItemCountThreshold(arg="to", max_items=5),
            )
        ],
    )
    assert _decide(Risk.WRITE, rules, "member", {"to": recipients}) is expected


def test_over_threshold_without_a_threshold_falls_back_to_requiring_approval() -> None:
    """A rule that says `over_threshold` but carries no threshold is a misconfiguration; it has
    to fail closed, or a half-written policy would silently waive every write."""
    rules = ApprovalRules(default_write="over_threshold")
    assert _decide(Risk.WRITE, rules, "owner") is True


def test_per_tool_override_beats_the_default() -> None:
    rules = ApprovalRules(
        default_write="always",
        overrides=[ApprovalOverride(tool="gmail__send_draft", rule="never")],
    )
    assert _decide(Risk.WRITE, rules, "owner") is False
    assert (
        needs_approval(
            risk=Risk.WRITE,
            tool_name="hubspot__create_note",
            args={},
            rules=rules,
            user_role="owner",
        )
        is True
    )


@pytest.mark.parametrize(
    "raw", [{}, {"default_write": "sometimes"}, {"overrides": "not-a-list"}, {"overrides": [{}]}]
)
def test_unparseable_rules_fall_back_to_the_safe_default(raw: dict) -> None:
    assert parse_approval_rules(raw) == ApprovalRules(default_write="always", overrides=[])


def test_parse_approval_rules_round_trips_a_valid_document() -> None:
    raw = {
        "default_write": "never",
        "overrides": [{"tool": "gmail__send_draft", "rule": "always"}],
    }
    rules = parse_approval_rules(raw)
    assert rules.default_write == "never"
    assert rules.for_tool("gmail__send_draft") == ("always", None)
    assert rules.for_tool("anything_else") == ("never", None)


def test_empty_allow_list_means_no_domain_restriction() -> None:
    assert blocked_recipients(["a@anywhere.com"], []) == []


def test_blocked_recipients_lists_only_disallowed_domains() -> None:
    allowed = ["Northstar.com", "@partner.io"]
    recipients = ["a@northstar.com", "b@PARTNER.IO", "c@evil.com"]
    assert blocked_recipients(recipients, allowed) == ["c@evil.com"]


def test_address_without_a_domain_is_blocked_when_a_list_is_configured() -> None:
    assert blocked_recipients(["not-an-address"], ["northstar.com"]) == ["not-an-address"]
