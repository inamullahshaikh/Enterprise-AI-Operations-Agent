"""The `google_calendar` connector (docs/system-design.md section 10.4).

Two reads and one write. `find_free_slots` exists as its own tool rather than leaving the model
to diff `list_events` against working hours itself: availability arithmetic over several
attendees is exactly the kind of thing a language model gets subtly wrong, and a wrong answer
here becomes a meeting invite at a time somebody is already busy.

Like `gmail`, this is backed by the mock service (section 21.2) until Phase 7 repoints
`config.base_url` at the real API with an OAuth token in `secrets`.
"""

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


class _Config(BaseModel):
    base_url: str
    default_calendar_id: str = "primary"


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
                return await self._dispatch(http, ctx, config, tool_name, args)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Calendar request failed: {exc}")

    async def _dispatch(
        self,
        http: httpx.AsyncClient,
        ctx: ExecutionContext,
        config: _Config,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolResult:
        if tool_name == "list_events":
            resp = await http.get(
                "/calendar/events",
                params={
                    k: v
                    for k, v in {
                        "time_min": args.get("time_min"),
                        "time_max": args.get("time_max"),
                        "calendar_id": args.get("calendar_id", config.default_calendar_id),
                    }.items()
                    if v is not None
                },
            )
            return _result(resp, meta_key="count")
        if tool_name == "find_free_slots":
            resp = await http.post(
                "/calendar/free-slots",
                json={
                    "attendees": args.get("attendees", []),
                    "duration_min": args["duration_min"],
                    "time_min": args.get("time_min"),
                    "time_max": args.get("time_max"),
                },
            )
            return _result(resp, meta_key="count")
        if tool_name == "create_event":
            resp = await http.post(
                "/calendar/events",
                json={
                    "title": args["title"],
                    "start": args["start"],
                    "end": args["end"],
                    "attendees": args.get("attendees", []),
                    "description": args.get("description", ""),
                    "calendar_id": args.get("calendar_id", config.default_calendar_id),
                },
                headers=_idempotency(ctx),
            )
            return _result(resp)
        return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        try:
            config = _Config.model_validate(ctx.config)
        except ValueError as exc:
            return False, f"Invalid configuration: {exc}"
        try:
            async with httpx.AsyncClient(base_url=config.base_url, timeout=_TIMEOUT_S) as http:
                resp = await http.get("/healthz")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"Cannot reach the calendar API: {exc}"
        return True, "Calendar API reachable"


def _auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    token = ctx.secrets.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _idempotency(ctx: ExecutionContext) -> dict[str, str]:
    return {"Idempotency-Key": ctx.idempotency_key} if ctx.idempotency_key else {}


def _result(resp: httpx.Response, *, meta_key: str | None = None) -> ToolResult:
    if resp.status_code >= 400:
        return ToolResult(ok=False, error=f"Calendar API returned {resp.status_code}: {resp.text}")
    content = resp.json()
    meta = {meta_key: len(content)} if meta_key and isinstance(content, list) else {}
    return ToolResult(ok=True, content=content, meta=meta)
