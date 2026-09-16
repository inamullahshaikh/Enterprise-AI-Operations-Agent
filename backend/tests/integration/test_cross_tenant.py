"""docs/system-design.md section 18.1 threat model row "Cross-tenant data
access" / section 26: "cross-tenant access returns 404 for every route." A
non-member must not be able to distinguish "workspace doesn't exist" from
"workspace exists but I'm not in it" — both are 404, never 403 or 200.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _register_and_get_token(client: AsyncClient, email: str) -> str:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": email},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_non_member_gets_404_not_403_or_200(client: AsyncClient) -> None:
    owner_token = await _register_and_get_token(client, "owner@example.com")
    create_resp = await client.post(
        "/api/v1/workspaces", json={"name": "Owner Co"}, headers=_auth(owner_token)
    )
    assert create_resp.status_code == 201
    workspace_id = create_resp.json()["id"]

    outsider_token = await _register_and_get_token(client, "outsider@example.com")

    get_resp = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=_auth(outsider_token))
    assert get_resp.status_code == 404

    members_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/members", headers=_auth(outsider_token)
    )
    assert members_resp.status_code == 404

    add_member_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "owner@example.com", "role": "member"},
        headers=_auth(outsider_token),
    )
    assert add_member_resp.status_code == 404


async def test_a_nonexistent_workspace_id_also_gets_404(client: AsyncClient) -> None:
    token = await _register_and_get_token(client, "solo@example.com")
    resp = await client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_member_with_viewer_role_cannot_manage_members(client: AsyncClient) -> None:
    owner_token = await _register_and_get_token(client, "owner2@example.com")
    create_resp = await client.post(
        "/api/v1/workspaces", json={"name": "Another Co"}, headers=_auth(owner_token)
    )
    workspace_id = create_resp.json()["id"]

    viewer_token = await _register_and_get_token(client, "viewer@example.com")
    add_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "viewer@example.com", "role": "viewer"},
        headers=_auth(owner_token),
    )
    assert add_resp.status_code == 201

    # A viewer is a real member (so 404-vs-403 doesn't apply) but lacks the
    # `admin` role required to manage members: 403, not 404.
    forbidden_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/members", headers=_auth(viewer_token)
    )
    assert forbidden_resp.status_code == 403


async def test_non_member_gets_404_on_conversations_and_runs(client: AsyncClient) -> None:
    owner_token = await _register_and_get_token(client, "conv-owner@example.com")
    ws_resp = await client.post(
        "/api/v1/workspaces", json={"name": "Conv Co"}, headers=_auth(owner_token)
    )
    workspace_id = ws_resp.json()["id"]
    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=_auth(owner_token)
    )
    conversation_id = conv_resp.json()["id"]

    outsider_token = await _register_and_get_token(client, "conv-outsider@example.com")
    outsider_headers = _auth(outsider_token)

    get_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}",
        headers=outsider_headers,
    )
    assert get_resp.status_code == 404

    send_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "hi"},
        headers=outsider_headers,
    )
    assert send_resp.status_code == 404


async def test_a_workspace_member_cannot_see_another_members_conversation(
    client: AsyncClient,
) -> None:
    """Conversations are private to the user who started them even for a real,
    same-workspace member — not just an outsider (docs/system-design.md section
    3: "Conversation - A chat thread"; ownership is per-user, membership alone
    isn't enough)."""
    owner_token = await _register_and_get_token(client, "shared-owner@example.com")
    ws_resp = await client.post(
        "/api/v1/workspaces", json={"name": "Shared Co"}, headers=_auth(owner_token)
    )
    workspace_id = ws_resp.json()["id"]
    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=_auth(owner_token)
    )
    conversation_id = conv_resp.json()["id"]

    member_token = await _register_and_get_token(client, "shared-member@example.com")
    add_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "shared-member@example.com", "role": "member"},
        headers=_auth(owner_token),
    )
    assert add_resp.status_code == 201

    get_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}",
        headers=_auth(member_token),
    )
    assert get_resp.status_code == 404
