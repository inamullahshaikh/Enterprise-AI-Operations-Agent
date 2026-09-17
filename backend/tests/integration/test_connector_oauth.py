"""Connecting a Google account to an installation (Phase 7 A3).

The token exchange goes to the mock service's `/oauth/token`, so nothing here talks to Google.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.security.credential_codec import decrypt_secrets
from relay_core.security.crypto import build_kms
from relay_core.security.jwt import create_state_token

pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace_with_gmail(
    client: AsyncClient, headers: dict[str, str], base_url: str = "http://mock-services:8100"
) -> tuple[str, str]:
    workspace = (
        await client.post("/api/v1/workspaces", json={"name": "Connected"}, headers=headers)
    ).json()
    resp = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/connectors",
        json={"connector_key": "gmail", "name": "Mailbox", "config": {"base_url": base_url}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return workspace["id"], resp.json()["id"]


async def _state_from_start(
    client: AsyncClient, headers: dict[str, str], workspace_id: str, installation_id: str
) -> str:
    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/oauth/start",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    query = parse_qs(urlparse(resp.json()["authorize_url"]).query)
    return query["state"][0]


async def test_start_returns_a_google_url_with_the_scopes_gmail_needs(
    client: AsyncClient,
) -> None:
    headers = await _register(client, "oauth-start@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, headers)

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/oauth/start",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    url = urlparse(resp.json()["authorize_url"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert query["scope"][0].split() == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    # Without both of these Google returns no refresh token, and the connection dies in an hour.
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]


async def test_a_member_cannot_start_a_connection(client: AsyncClient) -> None:
    owner_headers = await _register(client, "oauth-owner@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, owner_headers)
    member_headers = await _register(client, "oauth-member@example.com")
    added = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "oauth-member@example.com", "role": "member"},
        headers=owner_headers,
    )
    assert added.status_code == 201, added.text

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/oauth/start",
        headers=member_headers,
    )

    assert resp.status_code == 403, resp.text


async def test_the_callback_stores_the_token_and_discovers_the_tools(
    client: AsyncClient,
    db_session: AsyncSession,
    test_settings: Settings,
    mock_services_url: str,
) -> None:
    test_settings.google_token_url = f"{mock_services_url}/oauth/token"
    headers = await _register(client, "oauth-callback@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, headers, mock_services_url)
    state = await _state_from_start(client, headers, workspace_id, installation_id)

    resp = await client.get("/api/v1/oauth/callback", params={"state": state, "code": "granted"})

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].endswith(f"/connectors/{installation_id}?connected=1")

    credential = await ConnectorCredentialRepository(db_session).get(
        uuid.UUID(workspace_id), uuid.UUID(installation_id)
    )
    assert credential is not None and credential.oauth_expires_at is not None
    secrets = decrypt_secrets(build_kms(test_settings), credential)
    assert secrets["access_token"].startswith("mock-access-")
    assert secrets["refresh_token"].startswith("mock-refresh-")

    rows = await ToolDefinitionRepository(db_session).list_for_installation(
        uuid.UUID(workspace_id), uuid.UUID(installation_id)
    )
    assert {row.name for row in rows} == {
        "search_emails",
        "get_email",
        "create_draft",
        "send_draft",
    }


async def test_a_second_callback_with_the_same_state_is_refused(
    client: AsyncClient, test_settings: Settings, mock_services_url: str
) -> None:
    """The PKCE verifier is consumed on first use, so a replayed callback has nothing to exchange
    with."""
    test_settings.google_token_url = f"{mock_services_url}/oauth/token"
    headers = await _register(client, "oauth-replay@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, headers)
    state = await _state_from_start(client, headers, workspace_id, installation_id)

    first = await client.get("/api/v1/oauth/callback", params={"state": state, "code": "granted"})
    second = await client.get("/api/v1/oauth/callback", params={"state": state, "code": "granted"})

    assert first.headers["location"].endswith("?connected=1")
    assert second.headers["location"].endswith("?error=expired")


async def test_a_tampered_state_writes_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "oauth-tampered@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, headers)
    state = await _state_from_start(client, headers, workspace_id, installation_id)

    resp = await client.get(
        "/api/v1/oauth/callback", params={"state": state[:-4] + "aaaa", "code": "granted"}
    )

    assert resp.status_code == 400
    assert (
        await ConnectorCredentialRepository(db_session).get(
            uuid.UUID(workspace_id), uuid.UUID(installation_id)
        )
        is None
    )


async def test_a_state_naming_another_workspaces_installation_is_refused(
    client: AsyncClient, test_settings: Settings
) -> None:
    """The state is signed by us, so the check that matters is the one the route does anyway:
    the installation has to belong to the workspace the state claims."""
    headers = await _register(client, "oauth-crosstenant@example.com")
    _, installation_id = await _workspace_with_gmail(client, headers)
    other = (
        await client.post("/api/v1/workspaces", json={"name": "Other"}, headers=headers)
    ).json()
    state = create_state_token(
        {
            "workspace_id": other["id"],
            "installation_id": installation_id,
            "user_id": str(uuid.uuid4()),
            "nonce": "n",
        },
        settings=test_settings,
    )

    resp = await client.get("/api/v1/oauth/callback", params={"state": state, "code": "granted"})

    assert resp.status_code == 404, resp.text


async def test_a_denied_consent_screen_comes_back_as_an_error_banner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "oauth-denied@example.com")
    workspace_id, installation_id = await _workspace_with_gmail(client, headers)
    state = await _state_from_start(client, headers, workspace_id, installation_id)

    resp = await client.get(
        "/api/v1/oauth/callback", params={"state": state, "error": "access_denied"}
    )

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?error=access_denied")
    assert (
        await ConnectorCredentialRepository(db_session).get(
            uuid.UUID(workspace_id), uuid.UUID(installation_id)
        )
        is None
    )
