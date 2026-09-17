# MCP examples

## `ticketing/`

A small MCP server Relay has never seen, for the bring-your-own-tools demo
(docs/system-design.md sections 6.4 and 27.2 step 7) and for contract tests. It serves four tools
over streamable HTTP at `/mcp`:

| Tool | Annotations |
| --- | --- |
| `search_tickets(account_name?, status?, priority?)` | `readOnlyHint` |
| `get_ticket(ticket_id)` | `readOnlyHint` |
| `create_ticket(account_name, subject, priority, body)` | none |
| `add_comment(ticket_id, body)` | none |

Tickets are in memory and seeded for the same demo accounts as `mocks/main.py` (Acme Robotics,
Globex, Northwind Labs, Initech). `POST /_reset` restores them, and `GET /_stats` returns
`{"tickets_created": n, "comments_added": n}` so evals can count side effects.

### Run

```bash
docker compose up mcp-ticketing          # http://localhost:8200/mcp
# or, from source:
cd mcp_examples/ticketing && pip install -r requirements.txt && uvicorn server:app --port 8200
```

Set `MCP_TICKETING_TOKEN` to require `Authorization: Bearer <token>` on every request.

### Install it in Relay

`mcp-ticketing` resolves to a private address inside Docker, so the SSRF guard must allow it:
`SSRF_ALLOWED_HOSTS=["mock-services","mcp-ticketing"]` (already in `.env.example`). Then, as a
workspace admin:

```bash
curl -X POST http://localhost:8000/api/v1/workspaces/$WS/connectors \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"connector_key": "mcp", "name": "Ticketing", "config": {"url": "http://mcp-ticketing:8200/mcp"}}'
```

Add `"secrets": {"token": "..."}` if the server has a token. Relay discovers the four tools, and
the capability tagger proposes capabilities for them (for example `custom.ticket.read`). Every
tool starts as a `write`, so each call asks for approval until an admin lowers its risk with
`PATCH /workspaces/$WS/tools/{tool_id}`.
