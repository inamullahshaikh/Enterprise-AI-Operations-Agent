"""The `gmail` connector (docs/system-design.md section 10.3) — Relay's first connector that can
change something outside itself, and therefore the first one the approval gate actually gates.

**Draft-first.** There is no "send this text to this address" tool. Composing produces a *draft*
and sending an existing draft is a separate call, so the destructive step is always a distinct,
separately approvable action over content a human can already read. Both are `write`, so both
stop for approval under the default policy (section 13.1), but the split means an approver is
shown "send draft-3f2a" with the drafted text attached rather than being asked to eyeball
arguments the model just produced.

**Recipient guard.** `workspace_policies.email_domain_allow` is enforced here, in the connector,
because it's the only layer that knows which argument holds recipients — see
`ExecutionContext.policy`. A blocked recipient fails the call rather than silently dropping the
address, and it fails before the request is built, so nothing reaches Google.

**This speaks the real Gmail REST API** (Phase 7 B1). `config.base_url` is
`https://gmail.googleapis.com` in production and the mock service in dev and eval runs; the mock
answers the same paths and shapes, so there is one code path either way and no `if mock:` branch
to drift (ADR-0013 decision 1). The access token comes from `secrets`, refreshed on the way in
by `relay_core.connectors.oauth`.

**Gmail has no idempotency header**, so `ExecutionContext.idempotency_key` is not forwarded:
there is nothing on the far side to forward it to. What protects a crash between a send and its
checkpoint is Relay's own replay guard (ADR-0011), not upstream deduplication.
"""

import asyncio
import base64
import binascii
from email.message import EmailMessage
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
from relay_core.connectors.builtin.web_search import html_to_text
from relay_core.policy import blocked_recipients

_TIMEOUT_S = 15.0
# Gmail's list call returns ids only, so each hit costs a second request. The cap keeps a search
# at a bounded fan-out rather than letting the model ask for a hundred messages.
_MAX_RESULTS = 10
_USER = "me"


class _Config(BaseModel):
    base_url: str
    # Metadata only: the API always drafts as the authorized account (`users/me`). Kept so an
    # admin can record which mailbox an installation is connected to.
    sender_address: str | None = None


class GmailConnector(Connector):
    key = "gmail"
    display_name = "Gmail"
    auth_type = AuthType.OAUTH2

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search_emails",
                description=(
                    "Search the connected mailbox with Gmail search syntax (from:, subject:, "
                    "or bare words). Returns at most 10 message summaries without their full "
                    "bodies; each hit costs a separate fetch, so keep max_results small."
                ),
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 10},
                    },
                },
                risk=Risk.READ,
                capabilities=["email.read"],
            ),
            ToolSpec(
                name="get_email",
                description="Fetch one message in full, including its body, by id.",
                input_schema={
                    "type": "object",
                    "required": ["message_id"],
                    "properties": {"message_id": {"type": "string"}},
                },
                risk=Risk.READ,
                capabilities=["email.read"],
            ),
            ToolSpec(
                name="create_draft",
                description=(
                    "Compose an email and save it as a draft. This does NOT send it — "
                    "call send_draft with the returned draft_id to do that."
                ),
                input_schema={
                    "type": "object",
                    "required": ["to", "subject", "body"],
                    "properties": {
                        "to": {"type": "array", "items": {"type": "string"}},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "cc": {"type": "array", "items": {"type": "string"}},
                        "thread_id": {"type": "string"},
                    },
                },
                risk=Risk.WRITE,
                capabilities=["email.draft"],
                idempotent=False,
            ),
            ToolSpec(
                name="send_draft",
                description="Send a draft that was already created with create_draft.",
                input_schema={
                    "type": "object",
                    "required": ["draft_id"],
                    "properties": {"draft_id": {"type": "string"}},
                },
                risk=Risk.WRITE,
                capabilities=["email.send"],
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
                return await self._dispatch(http, ctx, tool_name, args)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Gmail request failed: {exc}")

    async def _dispatch(
        self,
        http: httpx.AsyncClient,
        ctx: ExecutionContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolResult:
        if tool_name == "search_emails":
            return await _search(http, args)
        if tool_name == "get_email":
            resp = await http.get(
                f"/gmail/v1/users/{_USER}/messages/{args['message_id']}", params={"format": "full"}
            )
            if (error := _error(resp)) is not None:
                return error
            return ToolResult(ok=True, content=_full_message(resp.json()))
        if tool_name == "create_draft":
            return await _create_draft(http, ctx, args)
        if tool_name == "send_draft":
            resp = await http.post(
                f"/gmail/v1/users/{_USER}/drafts/send", json={"id": args["draft_id"]}
            )
            if (error := _error(resp)) is not None:
                return error
            sent = resp.json()
            return ToolResult(
                ok=True,
                content={"message_id": sent.get("id"), "thread_id": sent.get("threadId")},
            )
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
                    f"/gmail/v1/users/{_USER}/messages", params={"maxResults": 1}
                )
        except httpx.HTTPError as exc:
            return False, f"Cannot reach the Gmail API: {exc}"
        if resp.status_code in (401, 403):
            return False, "Google rejected the token — reconnect this installation."
        if resp.status_code >= 400:
            return False, f"Gmail API returned {resp.status_code}"
        return True, "Gmail API reachable"


async def _search(http: httpx.AsyncClient, args: dict[str, Any]) -> ToolResult:
    max_results = max(1, min(int(args.get("max_results") or _MAX_RESULTS), _MAX_RESULTS))
    listed = await http.get(
        f"/gmail/v1/users/{_USER}/messages",
        params={"q": args["query"], "maxResults": max_results},
    )
    if (error := _error(listed)) is not None:
        return error
    ids = [m["id"] for m in listed.json().get("messages", [])]
    # Ids only come back from the list call, so every hit needs its own fetch. Concurrent, and
    # bounded by `_MAX_RESULTS` above.
    fetched = await asyncio.gather(*(_metadata(http, message_id) for message_id in ids))
    summaries = [summary for summary in fetched if summary is not None]
    return ToolResult(ok=True, content=summaries, meta={"count": len(summaries)})


async def _metadata(http: httpx.AsyncClient, message_id: str) -> dict[str, Any] | None:
    resp = await http.get(
        f"/gmail/v1/users/{_USER}/messages/{message_id}",
        params=[
            ("format", "metadata"),
            *(("metadataHeaders", name) for name in ("From", "To", "Subject", "Date")),
        ],
    )
    if resp.status_code >= 400:
        return None
    return _summary(resp.json())


async def _create_draft(
    http: httpx.AsyncClient, ctx: ExecutionContext, args: dict[str, Any]
) -> ToolResult:
    to, cc = args.get("to", []), args.get("cc", [])
    blocked = blocked_recipients(to + cc, ctx.policy.get("email_domain_allow", []))
    if blocked:
        return _domain_error(blocked)
    message: dict[str, Any] = {"raw": _raw_message(to, cc, args["subject"], args["body"])}
    if args.get("thread_id"):
        message["threadId"] = args["thread_id"]
    resp = await http.post(f"/gmail/v1/users/{_USER}/drafts", json={"message": message})
    if (error := _error(resp)) is not None:
        return error
    created = resp.json()
    return ToolResult(
        ok=True,
        content={
            "draft_id": created.get("id"),
            "message_id": (created.get("message") or {}).get("id"),
            "thread_id": (created.get("message") or {}).get("threadId"),
            "to": to,
            "cc": cc,
            "subject": args["subject"],
            "sent": False,
        },
    )


def _raw_message(to: list[str], cc: list[str], subject: str, body: str) -> str:
    """RFC 5322, base64url, unpadded — what Gmail wants in `draft.message.raw`. `EmailMessage`
    does the header encoding, so no hand-rolled MIME."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}


def _summary(message: dict[str, Any]) -> dict[str, Any]:
    headers = _headers(message.get("payload") or {})
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
    }


def _full_message(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    return _summary(message) | {"body": _body_text(payload)}


def _body_text(payload: dict[str, Any]) -> str:
    """First `text/plain` part, else `text/html` stripped to text. Multipart messages nest, so
    the walk is recursive; a single-part message carries its body on the payload itself."""
    mime = payload.get("mimeType", "")
    parts = payload.get("parts") or []
    if not parts:
        text = _decode(payload)
        return html_to_text(text) if mime == "text/html" else text
    for wanted in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") == wanted:
                text = _decode(part)
                return html_to_text(text) if wanted == "text/html" else text
    # multipart/alternative inside multipart/mixed, and so on.
    for part in parts:
        if (nested := _body_text(part)) != "":
            return nested
    return ""


def _decode(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
    except (binascii.Error, ValueError):
        return ""


def _auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    token = ctx.secrets.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _domain_error(blocked: list[str]) -> ToolResult:
    return ToolResult(
        ok=False,
        error=(
            f"Blocked by this workspace's email domain allow-list: {', '.join(blocked)}. "
            "Ask an admin to add the domain, or use a recipient that is already allowed."
        ),
    )


def _error(resp: httpx.Response) -> ToolResult | None:
    """Gmail's error envelope is `{"error": {"code", "message"}}`. A 401 or 403 means the token
    is gone or the scope was never granted, and the only fix is a human reconnecting — say that
    rather than handing the model a status code it will try to work around."""
    if resp.status_code < 400:
        return None
    if resp.status_code in (401, 403):
        return ToolResult(
            ok=False,
            error=(
                "Gmail rejected this request as unauthorized. The connected Google account "
                "needs to be reconnected by an admin before this tool can be used."
            ),
        )
    return ToolResult(ok=False, error=f"Gmail API returned {resp.status_code}: {_message(resp)}")


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message", ""))
    return resp.text[:500]
