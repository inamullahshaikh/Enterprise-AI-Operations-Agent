"""The `gmail` and `google_calendar` connectors against the real mock service
(docs/system-design.md sections 10.3, 10.4, 21.2).

The mock is started in-process on an ephemeral port and talked to over real HTTP rather than
being stubbed out with `respx`. That's deliberate: these connectors are thin HTTP adapters, so a
test that mocks the HTTP layer would be asserting that the code calls the functions it calls.
Running the actual `mocks/main.py` means the request shapes, the query parameters, the status
codes and the bearer check are all verified against something that answers the way Google does —
and it covers the mock service itself, which evals depend on being correct.

Since Phase 7 B1/B2 the connectors speak the real Gmail and Calendar REST APIs, so every call
below carries an access token and every path is a path Google serves. Pointing an installation
at Google instead of the mock is a `base_url` change and nothing else.

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
    base_url: str,
    *,
    allow: list[str] | None = None,
    token: str | None = "test-access-token",
    config: dict[str, object] | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        installation_id="gmail-test",
        config={"base_url": base_url, **(config or {})},
        secrets={"access_token": token} if token else {},
        policy={"email_domain_allow": allow or []},
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
    """Gmail's list call returns ids only, so `search_emails` is one list plus one fetch per hit
    — and the summaries it returns still carry no bodies."""
    connector = GmailConnector()
    ctx = _ctx(mock_services_url)

    found = await connector.call_tool(ctx, "search_emails", {"query": "renewal"})
    assert found.ok, found.error
    assert len(found.content) == 1
    assert found.content[0]["from"] == "jordan@acmerobotics.example"
    assert found.content[0]["subject"] == "Question about our renewal terms"
    assert "body" not in found.content[0]

    full = await connector.call_tool(ctx, "get_email", {"message_id": found.content[0]["id"]})
    assert full.ok, full.error
    assert "Acme Robotics" in full.content["body"]


async def test_gmail_query_syntax_reaches_the_api(mock_services_url: str) -> None:
    """`from:` is Gmail's own syntax, forwarded as `q` rather than reimplemented here."""
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url), "search_emails", {"query": "from:priya@globex.example"}
    )
    assert result.ok, result.error
    assert [m["from"] for m in result.content] == ["priya@globex.example"]


async def test_search_caps_the_fan_out(mock_services_url: str) -> None:
    """One fetch per hit means an unbounded `max_results` is an unbounded request count."""
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url), "search_emails", {"query": "", "max_results": 500}
    )
    assert result.ok, result.error
    assert result.meta["count"] <= 10


async def test_unknown_message_becomes_a_tool_error_not_an_exception(
    mock_services_url: str,
) -> None:
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url), "get_email", {"message_id": "nope"}
    )
    assert result.ok is False
    assert "404" in (result.error or "")


async def test_a_call_without_a_token_reports_reconnection(mock_services_url: str) -> None:
    """Google answers 401 for a missing or dead token. The model is told a human has to
    reconnect, rather than being handed a status code it will try to work around."""
    result = await GmailConnector().call_tool(
        _ctx(mock_services_url, token=None), "search_emails", {"query": "renewal"}
    )
    assert result.ok is False
    assert "reconnected" in (result.error or "")


async def test_draft_round_trips_through_the_message_api(mock_services_url: str) -> None:
    """The draft is built as RFC 5322 and sent base64url-encoded, so proving it survived means
    reading it back off the API and checking the decoded headers and body."""
    connector = GmailConnector()
    ctx = _ctx(mock_services_url)

    draft = await connector.call_tool(
        ctx,
        "create_draft",
        {
            "to": ["jordan@acmerobotics.example"],
            "cc": ["ops@northstar.example"],
            "subject": "Your renewal",
            "body": "Hi Jordan — your plan renews this month.",
        },
    )
    assert draft.ok, draft.error

    back = await connector.call_tool(ctx, "get_email", {"message_id": draft.content["message_id"]})
    assert back.ok, back.error
    assert back.content["subject"] == "Your renewal"
    assert "renews this month" in back.content["body"]
    assert "jordan@acmerobotics.example" in back.content["to"]

    sent = await connector.call_tool(ctx, "send_draft", {"draft_id": draft.content["draft_id"]})
    assert sent.ok, sent.error
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert len((await http.get("/gmail/sent")).json()) == 1


async def test_a_recipient_outside_the_allow_list_is_refused(mock_services_url: str) -> None:
    """Section 10.3's recipient guard, enforced in the connector because it's the only layer
    that knows which argument holds recipients — and enforced before the request is built, so
    nothing reaches Google."""
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
    assert created.content["attendees"] == ["priya@globex.example"]

    after = await connector.call_tool(ctx, "list_events", {})
    assert after.meta["count"] == 4
    titles = [e["title"] for e in after.content]
    assert "Renewal call — Globex" in titles
    round_tripped = next(e for e in after.content if e["title"] == "Renewal call — Globex")
    assert round_tripped["start"] == slots.content[0]["start"]
    assert round_tripped["end"] == slots.content[0]["end"]


async def test_free_slots_skip_a_busy_window(mock_services_url: str) -> None:
    """The arithmetic now runs over `freeBusy` blocks rather than raw events. A seeded meeting
    must not appear as free time for the person who is in it."""
    connector = GoogleCalendarConnector()
    ctx = _ctx(mock_services_url)

    events = await connector.call_tool(
        ctx, "list_events", {}
    )
    busy_event = next(
        e for e in events.content if "priya@globex.example" in e["attendees"]
    )
    slots = await connector.call_tool(
        ctx, "find_free_slots", {"attendees": ["priya@globex.example"], "duration_min": 60}
    )

    assert slots.ok, slots.error
    overlapping = [
        s
        for s in slots.content
        if s["start"] < busy_event["end"] and busy_event["start"] < s["end"]
    ]
    assert overlapping == []


async def test_create_event_defaults_to_not_mailing_anyone(mock_services_url: str) -> None:
    """A demo or eval run must not put invitations in real inboxes as a side effect, so
    `sendUpdates` defaults to `none` and only an explicit config changes it."""
    connector = GoogleCalendarConnector()

    result = await connector.call_tool(
        _ctx(mock_services_url),
        "create_event",
        {
            "title": "Check-in",
            "start": "2030-01-01T09:00:00+00:00",
            "end": "2030-01-01T09:30:00+00:00",
            "attendees": ["priya@globex.example"],
        },
    )
    assert result.ok, result.error
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert (await http.get("/_stats")).json()["last_send_updates"] == "none"

    explicit = await connector.call_tool(
        _ctx(mock_services_url, config={"send_updates": "all"}),
        "create_event",
        {
            "title": "Check-in with invites",
            "start": "2030-01-02T09:00:00+00:00",
            "end": "2030-01-02T09:30:00+00:00",
            "attendees": ["priya@globex.example"],
        },
    )
    assert explicit.ok, explicit.error
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert (await http.get("/_stats")).json()["last_send_updates"] == "all"


async def test_only_create_event_is_a_write(mock_services_url: str) -> None:
    specs = {s.name: s for s in await GoogleCalendarConnector().list_tools(_ctx(mock_services_url))}
    assert specs["list_events"].risk is Risk.READ
    assert specs["find_free_slots"].risk is Risk.READ
    assert specs["create_event"].risk is Risk.WRITE


async def test_health_check_reports_unreachable_rather_than_raising() -> None:
    ok, message = await GmailConnector().health_check(_ctx("http://127.0.0.1:1"))
    assert ok is False
    assert "Cannot reach" in message


async def test_health_check_says_not_connected_before_a_token_exists(
    mock_services_url: str,
) -> None:
    """An installation created without OAuth is legal (A3) but must never bind: the health check
    is what keeps it out of the resolver until someone connects an account."""
    for connector in (GmailConnector(), GoogleCalendarConnector()):
        ok, message = await connector.health_check(_ctx(mock_services_url, token=None))
        assert ok is False
        assert "Not connected" in message


async def test_health_check_passes_with_a_token(mock_services_url: str) -> None:
    for connector in (GmailConnector(), GoogleCalendarConnector()):
        ok, message = await connector.health_check(_ctx(mock_services_url))
        assert ok is True, message
