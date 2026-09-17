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
address.

**Backed by the mock service for now.** `config.base_url` points at `mocks/` (section 21.2), and
Phase 7 repoints it at `https://gmail.googleapis.com` with a real OAuth token in `secrets`. The
request shapes here are deliberately close to the real API so that swap stays small; what it is
*not* is a pretend implementation with no network call — every tool below really does HTTP.
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
from relay_core.policy import blocked_recipients

_TIMEOUT_S = 15.0


class _Config(BaseModel):
    base_url: str
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
                    "Search the connected mailbox. Matches sender, subject and body text. "
                    "Returns message summaries without their full bodies."
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
            resp = await http.get(
                "/gmail/messages",
                params={"q": args["query"], "max_results": args.get("max_results", 10)},
            )
            return _result(resp, meta_key="count")
        if tool_name == "get_email":
            resp = await http.get(f"/gmail/messages/{args['message_id']}")
            return _result(resp)
        if tool_name == "create_draft":
            blocked = _blocked(ctx, args.get("to", []) + args.get("cc", []))
            if blocked:
                return _domain_error(blocked)
            resp = await http.post(
                "/gmail/drafts",
                json={
                    "to": args["to"],
                    "subject": args["subject"],
                    "body": args["body"],
                    "cc": args.get("cc", []),
                    "thread_id": args.get("thread_id"),
                },
                headers=_idempotency(ctx),
            )
            return _result(resp)
        if tool_name == "send_draft":
            resp = await http.post(
                f"/gmail/drafts/{args['draft_id']}/send", headers=_idempotency(ctx)
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
            return False, f"Cannot reach the mailbox API: {exc}"
        return True, "Mailbox API reachable"


def _auth_headers(ctx: ExecutionContext) -> dict[str, str]:
    token = ctx.secrets.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _idempotency(ctx: ExecutionContext) -> dict[str, str]:
    return {"Idempotency-Key": ctx.idempotency_key} if ctx.idempotency_key else {}


def _blocked(ctx: ExecutionContext, recipients: list[str]) -> list[str]:
    return blocked_recipients(recipients, ctx.policy.get("email_domain_allow", []))


def _domain_error(blocked: list[str]) -> ToolResult:
    return ToolResult(
        ok=False,
        error=(
            f"Blocked by this workspace's email domain allow-list: {', '.join(blocked)}. "
            "Ask an admin to add the domain, or use a recipient that is already allowed."
        ),
    )


def _result(resp: httpx.Response, *, meta_key: str | None = None) -> ToolResult:
    if resp.status_code >= 400:
        return ToolResult(ok=False, error=f"Gmail API returned {resp.status_code}: {resp.text}")
    content = resp.json()
    meta = {meta_key: len(content)} if meta_key and isinstance(content, list) else {}
    return ToolResult(ok=True, content=content, meta=meta)
