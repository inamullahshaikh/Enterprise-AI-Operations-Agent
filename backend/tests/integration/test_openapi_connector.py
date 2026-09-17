"""The `openapi` connector (Phase 6 B5): preview the mock service's own spec, install two of its
operations, and call them."""

import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.openapi_connector import OpenApiConnector
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("ssrf_allows_localhost")]


async def _install(client: AsyncClient, mock_services_url: str, extra: list[dict] | None = None):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"openapi-{uuid.uuid4().hex[:6]}@example.com",
            "password": "correct horse battery staple",
            "full_name": "Dev",
        },
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.post("/api/v1/workspaces", json={"name": "API"}, headers=headers)).json()
    api = f"/api/v1/workspaces/{ws['id']}/connectors"

    resp = await client.post(
        f"{api}/openapi/preview",
        json={"spec_url": f"{mock_services_url}/openapi.json"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    by_path = {(op["method"], op["path"]): op for op in resp.json()["operations"]}
    search = by_path[("get", "/gmail/messages")]
    draft = {**by_path[("post", "/gmail/drafts")], "risk": "read"}  # a client-sent risk is ignored

    resp = await client.post(
        api,
        json={
            "connector_key": "openapi",
            "name": "Mailbox API",
            "config": {
                "base_url": mock_services_url,
                "operations": [search, draft, *(extra or [])],
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    installation = resp.json()
    assert installation["health"] == "healthy", installation
    return uuid.UUID(ws["id"]), installation, search["name"], draft["name"]


def _ctx(installation: dict, idempotency_key: str | None = None) -> ExecutionContext:
    installation_id = uuid.UUID(installation["id"])
    return ExecutionContext(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=installation_id,
        conversation_id=installation_id,
        installation_id=str(installation_id),
        config=installation["config"],
        idempotency_key=idempotency_key,
    )


async def test_selected_operations_become_rows_and_the_get_returns_seeded_data(
    client: AsyncClient, db_session: AsyncSession, mock_services_url: str
) -> None:
    workspace_id, installation, search, draft = await _install(client, mock_services_url)

    rows = await ToolDefinitionRepository(db_session).list_for_installation(
        workspace_id, uuid.UUID(installation["id"])
    )
    assert {r.name: r.risk for r in rows} == {search: "read", draft: "write"}

    result = await OpenApiConnector().call_tool(_ctx(installation), search, {"q": "renewal"})
    assert result.ok, result.error
    assert [m["from"] for m in result.content] == ["jordan@acmerobotics.example"]


async def test_a_path_argument_cannot_walk_to_another_endpoint(
    client: AsyncClient, mock_services_url: str
) -> None:
    poke = {
        "name": "poke_draft",
        "method": "post",
        "path": "/gmail/drafts/{draft_id}",
        "input_schema": {"type": "object", "properties": {"draft_id": {"type": "string"}}},
        "params": [{"name": "draft_id", "location": "path"}],
    }
    _, installation, _, draft = await _install(client, mock_services_url, extra=[poke])
    connector = OpenApiConnector()
    body = {"to": ["jordan@acmerobotics.example"], "subject": "Hi", "body": "Hello"}
    assert (await connector.call_tool(_ctx(installation), draft, {"body": body})).ok

    # Unencoded, this would be POST /gmail/drafts/../../_reset, i.e. POST /_reset.
    result = await connector.call_tool(
        _ctx(installation), "poke_draft", {"draft_id": "../../_reset"}
    )
    assert result.ok is False
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert len((await http.get("/gmail/drafts")).json()) == 1


async def test_a_post_forwards_the_idempotency_key(
    client: AsyncClient, mock_services_url: str
) -> None:
    _, installation, _, draft = await _install(client, mock_services_url)
    connector = OpenApiConnector()
    body = {"to": ["priya@globex.example"], "subject": "Seats", "body": "Hello"}

    first = await connector.call_tool(_ctx(installation, "key-1"), draft, {"body": body})
    again = await connector.call_tool(_ctx(installation, "key-1"), draft, {"body": body})

    assert first.ok and again.ok
    assert first.content["draft_id"] == again.content["draft_id"]
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        assert len((await http.get("/gmail/drafts")).json()) == 1


async def test_an_absolute_operation_path_is_refused(
    client: AsyncClient, mock_services_url: str
) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "openapi-abs@example.com",
            "password": "correct horse battery staple",
            "full_name": "D",
        },
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.post("/api/v1/workspaces", json={"name": "Abs"}, headers=headers)).json()
    evil = {"name": "evil", "method": "get", "path": "//169.254.169.254/latest", "input_schema": {}}
    resp = await client.post(
        f"/api/v1/workspaces/{ws['id']}/connectors",
        json={
            "connector_key": "openapi",
            "name": "Evil",
            "config": {"base_url": mock_services_url, "operations": [evil]},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
