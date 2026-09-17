"""The `gmail` and `google_calendar` connectors against the real mock service
(docs/system-design.md sections 10.3, 10.4, 21.2).

The mock is started in-process on an ephemeral port and talked to over real HTTP rather than
being stubbed out with `respx`. That's deliberate: these connectors are thin HTTP adapters, so a
test that mocks the HTTP layer would be asserting that the code calls the functions it calls.
Running the actual `mocks/main.py` means the request shapes, the query parameters, the status
codes and the `Idempotency-Key` handling are all checked against the thing that will really
answer them — and it covers the mock service itself, which evals depend on being correct.

The `mock_services_url` fixture lives in `conftest.py`, shared with the eval harness's
approval-compliance test.
"""

import uuid

import httpx
import pytest

from relay_core.connectors.base import ExecutionContext, Risk
from relay_core.connectors.builtin.gmail import GmailConnector
from relay_core.connectors.builtin.google_calendar import GoogleCalendarConnector

pytestmark = pytest.mark.asyncio


def _ctx(
    base_url: str, *, allow: list[str] | None = None, key: str | None = None
) -> ExecutionContext:
    return ExecutionContext(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        installation_id="gmail-test",
        config={"base_url": base_url},
        policy={"email_domain_allow": allow or []},
        idempotency_key=key,
    )


async def test_gmail_tools_declare_drafting_and_sending_as_separate_writes(
    mock_services_url: str,
) -> None:
    """Section 10.3's draft-first design: composing and sending are distinct tools, both `write`,
    so each stops for its own approval and neither can happen as a side effect of the other."""
    specs = {s.name: s for s in await GmailConnector().list_tools(_ctx(mock_services_url))}
    assert specs["search_emails"].risk is Risk.READ
    assert specs["get_email"].risk is Risk.READ
    assert specs["create_draft"].risk is Risk.WRITE
    assert specs["send_draft"].risk is Risk.WRITE
    # Non-idempotent, so `ToolExecutor` never silently retries a send on a transport error.
    assert specs["create_draft"].idempotent is False
    assert specs["send_draft"].idempotent is False


async def test_search_then_read_one_message(mock_services_url: str) -> None:
    connector = GmailConnector()
    ctx = _ctx(mock_services_url)

    found = await connector.call_tool(ctx, "search_emails", {"query": "renewal"})
    assert found.ok, found.error
    assert len(found.content) == 1
    assert found.content[0]["from"] == "jordan@acmerobotics.example"
    # Search returns summaries only — bodies come from get_email.
    assert "body" not in found.content[0]

    full = await connector.call_tool(ctx, "get_email", {"message_id": found.content[0]["id"]})
    assert full.ok, full.error
    assert "Acme Robotics" in full.content["body"]


async def test_unknown_message_becomes_a_tool_error_not_an_exception(
    mock_services_url: str,
) -> None:
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url), "get_email", {"message_id": "nope"}
    )
    assert result.ok is False
    assert "404" in (result.error or "")


async def test_draft_then_send(mock_services_url: str) -> None:
    connector = GmailConnector()
    ctx = _ctx(mock_services_url)

    draft = await connector.call_tool(
        ctx,
        "create_draft",
        {
            "to": ["jordan@acmerobotics.example"],
            "subject": "Your renewal",
            "body": "Hi Jordan — your plan renews this month.",
        },
    )
    assert draft.ok, draft.error
    assert draft.content["sent"] is False

    sent = await connector.call_tool(ctx, "send_draft", {"draft_id": draft.content["draft_id"]})
    assert sent.ok, sent.error
    assert sent.content["to"] == ["jordan@acmerobotics.example"]

    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert len((await http.get("/gmail/sent")).json()) == 1


async def test_the_same_idempotency_key_does_not_send_twice(mock_services_url: str) -> None:
    """The upstream half of section 13.3. Relay's own replay guard covers a crash between the
    call and its checkpoint; this covers the request actually reaching the far side twice."""
    connector = GmailConnector()
    draft = await connector.call_tool(
        _ctx(mock_services_url),
        "create_draft",
        {"to": ["priya@globex.example"], "subject": "Renewal", "body": "Hello."},
    )
    draft_id = draft.content["draft_id"]
    ctx = _ctx(mock_services_url, key="sha256-of-a-tool-call")

    first = await connector.call_tool(ctx, "send_draft", {"draft_id": draft_id})
    second = await connector.call_tool(ctx, "send_draft", {"draft_id": draft_id})

    assert first.ok and second.ok
    assert first.content["message_id"] == second.content["message_id"]
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert len((await http.get("/gmail/sent")).json()) == 1


async def test_a_recipient_outside_the_allow_list_is_refused(mock_services_url: str) -> None:
    """Section 10.3's recipient guard, enforced in the connector because it's the only layer
    that knows which argument holds recipients."""
    ctx = _ctx(mock_services_url, allow=["acmerobotics.example"])

    result = await GmailConnector().call_tool(
        ctx,
        "create_draft",
        {
            "to": ["jordan@acmerobotics.example", "stranger@evil.example"],
            "subject": "x",
            "body": "y",
        },
    )

    assert result.ok is False
    assert "stranger@evil.example" in (result.error or "")
    # Nothing was written — the guard refuses the whole call rather than dropping one address.
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert (await http.get("/gmail/drafts")).json() == []


async def test_an_empty_allow_list_permits_any_recipient(mock_services_url: str) -> None:
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url, allow=[]),
        "create_draft",
        {"to": ["anyone@wherever.example"], "subject": "x", "body": "y"},
    )
    assert result.ok, result.error


async def test_calendar_read_find_and_create(mock_services_url: str) -> None:
    connector = GoogleCalendarConnector()
    ctx = _ctx(mock_services_url)

    events = await connector.call_tool(ctx, "list_events", {})
    assert events.ok, events.error
    assert events.meta["count"] == 3

    slots = await connector.call_tool(
        ctx, "find_free_slots", {"attendees": ["priya@globex.example"], "duration_min": 30}
    )
    assert slots.ok, slots.error
    assert slots.content, "expected at least one free slot in the next five working days"

    created = await connector.call_tool(
        ctx,
        "create_event",
        {
            "title": "Renewal call — Globex",
            "start": slots.content[0]["start"],
            "end": slots.content[0]["end"],
            "attendees": ["priya@globex.example"],
        },
    )
    assert created.ok, created.error

    after = await connector.call_tool(ctx, "list_events", {})
    assert after.meta["count"] == 4


async def test_only_create_event_is_a_write(mock_services_url: str) -> None:
    specs = {s.name: s for s in await GoogleCalendarConnector().list_tools(_ctx(mock_services_url))}
    assert specs["list_events"].risk is Risk.READ
    assert specs["find_free_slots"].risk is Risk.READ
    assert specs["create_event"].risk is Risk.WRITE


async def test_health_check_reports_unreachable_rather_than_raising() -> None:
    ok, message = await GmailConnector().health_check(_ctx("http://127.0.0.1:1"))
    assert ok is False
    assert "Cannot reach" in message
