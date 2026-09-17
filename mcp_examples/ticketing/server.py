"""A sample MCP ticketing server Relay has never seen (docs/system-design.md sections 6.4, 24,
27.2 step 7). It exists to prove the bring-your-own-tools claim: point an `mcp` installation at
it and the agent can search and create tickets with no Relay code change.

Built on the official `mcp` SDK's `MCPServer` (FastMCP's mcp 2.x name) over streamable HTTP at
`/mcp`. State is in-memory and seeded from the same demo accounts as `mocks/main.py`.

- `MCP_TICKETING_TOKEN`: when set, every request needs `Authorization: Bearer <token>`.
- `POST /_reset` and `GET /_stats` are for evals and tests, like the mock service's `/_reset`.
"""

import os
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

Priority = Literal["P1", "P2", "P3"]
Status = Literal["open", "pending", "closed"]

_SEED = [
    ("Acme Robotics", "Fleet dashboard not loading", "P1", "open"),
    ("Acme Robotics", "Invoice shows wrong seat count", "P2", "pending"),
    ("Acme Robotics", "SSO login loop after renewal", "P1", "open"),
    ("Globex", "Export to CSV times out", "P2", "open"),
    ("Globex", "Request for additional admin seats", "P3", "closed"),
    ("Northwind Labs", "Usage report missing last week", "P1", "open"),
    ("Northwind Labs", "API rate limit questions", "P3", "open"),
    ("Initech", "Downgrade to starter plan", "P2", "pending"),
    ("Initech", "Password reset emails delayed", "P2", "closed"),
    ("Initech", "Webhook deliveries failing", "P1", "closed"),
]


class _Store:
    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        for i, (account, subject, priority, status) in enumerate(_SEED, start=1):
            ticket_id = f"TCK-{1000 + i}"
            self.tickets[ticket_id] = {
                "ticket_id": ticket_id,
                "account_name": account,
                "subject": subject,
                "priority": priority,
                "status": status,
                "body": f"{account} reported: {subject.lower()}.",
                "comments": [],
            }
        self.tickets_created = 0
        self.comments_added = 0


store = _Store()
mcp = MCPServer("ticketing", instructions="Customer support tickets for Northstar accounts.")
_READ = ToolAnnotations(read_only_hint=True)


def _summary(ticket: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ticket.items() if k not in ("body", "comments")}


@mcp.tool(annotations=_READ)
def search_tickets(
    account_name: str | None = None,
    status: Status | None = None,
    priority: Priority | None = None,
) -> list[dict[str, Any]]:
    """Search support tickets. Every filter is optional; account_name matches case-insensitively
    on a substring."""
    return [
        _summary(t)
        for t in store.tickets.values()
        if (not account_name or account_name.lower() in t["account_name"].lower())
        and (not status or t["status"] == status)
        and (not priority or t["priority"] == priority)
    ]


@mcp.tool(annotations=_READ)
def get_ticket(ticket_id: str) -> dict[str, Any]:
    """Get one ticket, including its body and comments."""
    ticket = store.tickets.get(ticket_id)
    if ticket is None:
        raise ToolError(f"No ticket {ticket_id}")
    return ticket


@mcp.tool()
def create_ticket(account_name: str, subject: str, priority: Priority, body: str) -> dict[str, Any]:
    """Open a new support ticket for an account."""
    ticket_id = f"TCK-{1001 + len(store.tickets)}"
    ticket = {
        "ticket_id": ticket_id,
        "account_name": account_name,
        "subject": subject,
        "priority": priority,
        "status": "open",
        "body": body,
        "comments": [],
    }
    store.tickets[ticket_id] = ticket
    store.tickets_created += 1
    return _summary(ticket)


@mcp.tool()
def add_comment(ticket_id: str, body: str) -> dict[str, Any]:
    """Add a comment to an existing ticket."""
    ticket = store.tickets.get(ticket_id)
    if ticket is None:
        raise ToolError(f"No ticket {ticket_id}")
    comment = {"body": body, "created_at": datetime.now(UTC).isoformat()}
    ticket["comments"].append(comment)
    store.comments_added += 1
    return {"ticket_id": ticket_id, **comment}


@mcp.custom_route("/_reset", methods=["POST"])
async def reset(request: Request) -> Response:
    global store
    store = _Store()
    return JSONResponse({"status": "reset"})


@mcp.custom_route("/_stats", methods=["GET"])
async def stats(request: Request) -> Response:
    return JSONResponse(
        {"tickets_created": store.tickets_created, "comments_added": store.comments_added}
    )


class _BearerAuth:
    """Rejects any HTTP request without the expected bearer token. `/_reset` and `/_stats`
    included: they are test affordances, not a way around the token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and dict(scope["headers"]).get(b"authorization") != self.expected
        ):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app() -> ASGIApp:
    # host="0.0.0.0" leaves the SDK's localhost-only Host check off: in Docker, Relay calls this
    # server as `mcp-ticketing:8200`, a Host header that check would reject.
    app: ASGIApp = mcp.streamable_http_app(host="0.0.0.0")
    token = os.environ.get("MCP_TICKETING_TOKEN")
    return _BearerAuth(app, token) if token else app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)
