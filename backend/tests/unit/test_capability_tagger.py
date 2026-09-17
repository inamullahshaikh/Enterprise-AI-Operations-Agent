"""The capability tagger (Phase 6 C1): the model proposes, code decides."""

import uuid
from typing import Any

from relay_core.capabilities.tagger import _TagBatch, _ToolTag, tag_tools
from relay_core.connectors.base import Risk, ToolSpec
from tests.unit.test_agent_nodes import FakeGateway


def _mcp_tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Search support tickets.",
        input_schema={"type": "object", "properties": {}},
        risk=Risk.WRITE,
        idempotent=False,
        read_only_hint=True,
    )


async def _tag(tag: _ToolTag) -> Any:
    gateway = FakeGateway(parsed=_TagBatch(tools=[tag]))
    results = await tag_tools(gateway, None, uuid.uuid4(), [_mcp_tool(tag.name)])  # type: ignore[arg-type]
    return results[tag.name]


async def test_an_invalid_capability_is_dropped() -> None:
    result = await _tag(
        _ToolTag(
            name="search_tickets",
            capabilities=["custom.ticket.read", "tickets", "Custom.Bad", "sql.query"],
            suggested_risk="read",
            confidence=0.9,
        )
    )
    assert result.capabilities == ["custom.ticket.read", "sql.query"]
    assert result.needs_review is False


async def test_a_suggested_read_never_lowers_an_mcp_write() -> None:
    result = await _tag(
        _ToolTag(
            name="search_tickets",
            capabilities=["custom.ticket.read"],
            suggested_risk="read",
            confidence=0.9,
        )
    )
    assert result.risk is Risk.WRITE


async def test_a_suggested_destructive_raises_the_risk() -> None:
    result = await _tag(
        _ToolTag(
            name="search_tickets", capabilities=[], suggested_risk="destructive", confidence=1.5
        )
    )
    assert result.risk is Risk.DESTRUCTIVE
    assert result.confidence == 1.0
    assert result.needs_review is True  # no capabilities


async def test_low_confidence_needs_review() -> None:
    result = await _tag(
        _ToolTag(
            name="search_tickets",
            capabilities=["custom.ticket.read"],
            suggested_risk="write",
            confidence=0.5,
        )
    )
    assert result.needs_review is True


async def test_a_gateway_failure_leaves_every_tool_untagged() -> None:
    class Failing:
        async def generate(self, **_: Any) -> Any:
            raise RuntimeError("quota")

    results = await tag_tools(Failing(), None, uuid.uuid4(), [_mcp_tool("search_tickets")])  # type: ignore[arg-type]
    assert results == {}
