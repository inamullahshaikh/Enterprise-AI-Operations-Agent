"""The `google_calendar` connector (docs/system-design.md section 10.4).

Two reads and one write. `find_free_slots` exists as its own tool rather than leaving the model
to diff `list_events` against working hours itself: availability arithmetic over several
attendees is exactly the kind of thing a language model gets subtly wrong, and a wrong answer
here becomes a meeting invite at a time somebody is already busy. It runs over Google's
`freeBusy` response, which is the same arithmetic over a simpler input than raw events.

**This speaks the real Calendar REST API** (Phase 7 B2). `config.base_url` is
`https://www.googleapis.com` in production and the mock service in dev and eval runs — one code
path either way (ADR-0013 decision 1).

Two defaults worth knowing about:

- `send_updates` defaults to `none`, so creating an event never mails an invitation to a real
  person as a side effect of a demo or an eval. An admin who wants invites sets it explicitly.
- The connector always sends an explicit `timeZone` (`config.time_zone`, default `UTC`) rather
  than inheriting the calendar's, so a free-slot answer and the event created from it are in the
  same zone.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)

_TIMEOUT_S = 15.0
# The working window `find_free_slots` searches: 09:00-17:00 over the next five days. Deliberately
# simple — a stable, checkable answer rather than a scheduling engine.
_DAY_START_HOUR = 9
_DAY_END_HOUR = 17
_DAYS_AHEAD = 5
_MAX_SLOTS = 10


class _Config(BaseModel):
    base_url: str
    default_calendar_id: str = "primary"
    time_zone: str = "UTC"
    send_updates: str = "none"


class GoogleCalendarConnector(Connector):
    key = "google_calendar"
    display_name = "Google Calendar"
    auth_type = AuthType.OAUTH2

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_events",
                description="List calendar events overlapping a time window (RFC 3339 times).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "time_min": {"type": "string"},
                        "time_max": {"type": "string"},
                        "calendar_id": {"type": "string"},
                    },
                },
                risk=Risk.READ,
                capabilities=["calendar.read"],
            ),
            ToolSpec(
                name="find_free_slots",
                description=(
                    "Find times in the next few working days when every listed attendee is "
                    "free. Use this instead of reasoning over list_events yourself."
                ),
                input_schema={
                    "type": "object",
                    "required": ["duration_min"],
                    "properties": {
                        "attendees": {"type": "array", "items": {"type": "string"}},
                        "duration_min": {"type": "integer"},
                        "time_min": {"type": "string"},
                        "time_max": {"type": "string"},
                    },
                },
                risk=Risk.READ,
                capabilities=["calendar.read"],
            ),
            ToolSpec(
                name="create_event",
                description="Create a calendar event and invite attendees.",
                input_schema={
                    "type": "object",
                    "required": ["title", "start", "end"],
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "attendees": {"type": "array", "items": {"type": "string"}},
                        "description": {"type": "string"},
                        "calendar_id": {"type": "string"},
                    },
                },
                risk=Risk.WRITE,
                capabilities=["calendar.write"],
                idempotent=False,
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        config = _Config.model_validate(ctx.config)
        try:
            async with httpx.AsyncClient(
                base_url=config.base_url, timeout=_TIMEOUT_S, headers=_auth_headers(ctx)
            ) as http:
                return await self._dispatch(http, config, tool_name, args)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Calendar request failed: {exc}")

    async def _dispatch(
        self,
        http: httpx.AsyncClient,
        config: _Config,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolResult:
        if tool_name == "list_events":
            calendar_id = args.get("calendar_id") or config.default_calendar_id
            resp = await http.get(
                f"/calendar/v3/calendars/{calendar_id}/events",
                params={
                    k: v
                    for k, v in {
                        "timeMin": args.get("time_min"),
                        "timeMax": args.get("time_max"),
                        "singleEvents": "true",
                        "orderBy": "startTime",
                    }.items()
                    if v is not None
                },
            )
            if (error := _error(resp)) is not None:
                return error
            events = [_event(item) for item in resp.json().get("items", [])]
            return ToolResult(ok=True, content=events, meta={"count": len(events)})
        if tool_name == "find_free_slots":
            return await _free_slots(http, args)
        if tool_name == "create_event":
            return await _create_event(http, config, args)
        return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        try:
            config = _Config.model_validate(ctx.config)
        except ValueError as exc:
            return False, f"Invalid configuration: {exc}"
        if not ctx.secrets.get("access_token"):
            return False, "Not connected — connect a Google account to this installation."
        try:
            async with httpx.AsyncClient(
                base_url=config.base_url, timeout=_TIMEOUT_S, headers=_auth_headers(ctx)
            ) as http:
                resp = await http.get(
                    f"/calendar/v3/calendars/{config.default_calendar_id}/events",
                    params={"maxResults": 1},
                )
        except httpx.HTTPError as exc:
            return False, f"Cannot reach the Calendar API: {exc}"
        if resp.status_code in (401, 403):
            return False, "Google rejected the token — reconnect this installation."
        if resp.status_code >= 400:
            return False, f"Calendar API returned {resp.status_code}"
        return True, "Calendar API reachable"


async def _free_slots(http: httpx.AsyncClient, args: dict[str, Any]) -> ToolResult:
    duration = timedelta(minutes=int(args["duration_min"]))
    window_start, window_end = _window(args)
    attendees = args.get("attendees", [])
    resp = await http.post(
        "/calendar/v3/freeBusy",
        json={
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "items": [{"id": address} for address in attendees] or [{"id": "primary"}],
        },
    )
    if (error := _error(resp)) is not None:
        return error
    busy = [
        (block.get("start", ""), block.get("end", ""))
        for calendar in resp.json().get("calendars", {}).values()
        for block in calendar.get("busy", [])
    ]
    slots = _slots(window_start, window_end, duration, busy)
    return ToolResult(ok=True, content=slots, meta={"count": len(slots)})


def _window(args: dict[str, Any]) -> tuple[datetime, datetime]:
    start = _parse(args.get("time_min")) or datetime.now(UTC).replace(
        hour=_DAY_START_HOUR, minute=0, second=0, microsecond=0
    )
    end = _parse(args.get("time_max")) or start + timedelta(days=_DAYS_AHEAD)
    return start, end


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _slots(
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    busy: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """Walk working hours in `duration` steps and keep the steps no busy block overlaps."""
    blocks = [(s, e) for s, e in ((_parse(s), _parse(e)) for s, e in busy) if s and e]
    slots: list[dict[str, str]] = []
    day = window_start.replace(minute=0, second=0, microsecond=0)
    for offset in range(_DAYS_AHEAD + 1):
        cursor = max(
            window_start, (day + timedelta(days=offset)).replace(hour=_DAY_START_HOUR)
        )
        end_of_day = (day + timedelta(days=offset)).replace(hour=_DAY_END_HOUR)
        while cursor + duration <= min(end_of_day, window_end):
            slot_end = cursor + duration
            if not any(start < slot_end and cursor < end for start, end in blocks):
                slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
                if len(slots) == _MAX_SLOTS:
                    return slots
            cursor = slot_end
    return slots


async def _create_event(
    http: httpx.AsyncClient, config: _Config, args: dict[str, Any]
) -> ToolResult:
    calendar_id = args.get("calendar_id") or config.default_calendar_id
    resp = await http.post(
        f"/calendar/v3/calendars/{calendar_id}/events",
        params={"sendUpdates": config.send_updates},
        json={
            "summary": args["title"],
            "description": args.get("description", ""),
            "start": {"dateTime": args["start"], "timeZone": config.time_zone},
            "end": {"dateTime": args["end"], "timeZone": config.time_zone},
            "attendees": [{"email": address} for address in args.get("attendees", [])],
        },
    )
    if (error := _error(resp)) is not None:
        return error
    return ToolResult(ok=True, content=_event(resp.json()))


def _event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "title": item.get("summary", ""),
        "description": item.get("description", ""),
        "start": (item.get("start") or {}).get("dateTime", ""),
        "end": (item.get("end") or {}).get("dateTime", ""),
        "attendees": [a.get("email", "") for a in item.get("attendees", [])],
        "html_link": item.get("htmlLink"),
    }


def _auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    token = ctx.secrets.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _error(resp: httpx.Response) -> ToolResult | None:
    if resp.status_code < 400:
        return None
    if resp.status_code in (401, 403):
        return ToolResult(
            ok=False,
            error=(
                "Google Calendar rejected this request as unauthorized. The connected Google "
                "account needs to be reconnected by an admin before this tool can be used."
            ),
        )
    return ToolResult(ok=False, error=f"Calendar API returned {resp.status_code}: {_message(resp)}")


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message", ""))
    return resp.text[:500]
