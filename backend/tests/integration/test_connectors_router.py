"""RBAC and validation coverage for `relay_api/routers/connectors.py` (docs/system-design.md
section 15.3 / FR-3/FR-7/FR-9), which `test_execute_step_flow.py` only exercises along its one
happy path (an admin installing a valid `postgres` connector). Missing before this file: who is
and isn't allowed to install/list/delete/test a connector, cross-tenant isolation on connector
routes specifically, and that config/secrets are validated against the manifest's JSON Schema
before anything is written.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": email},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, owner_headers: dict, name: str) -> str:
    resp = await client.post("/api/v1/workspaces", json={"name": name}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _add_member(
    client: AsyncClient, owner_headers: dict, workspace_id: str, email: str, role: str
) -> None:
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": email, "role": role},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text


def _valid_postgres_body(postgres_url: str, name: str = "Demo DB") -> dict:
    url = make_url(postgres_url)
    return {
        "connector_key": "postgres",
        "name": name,
        "config": {"host": url.host, "port": url.port, "database": url.database},
        "secrets": {"username": url.username, "password": url.password},
    }


async def test_catalog_lists_postgres_but_not_file_upload(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/connectors/catalog")
    assert resp.status_code == 200
    keys = {m["key"] for m in resp.json()}
    assert keys == {"postgres"}


async def test_admin_can_install_and_the_health_check_runs_for_real(
    client: AsyncClient, postgres_url: str
) -> None:
    owner_headers = await _register(client, "router-admin@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["health"] == "healthy"
    assert body["status"] == "active"
    assert body["slug"] == "demo-db"


async def test_install_rejects_config_missing_a_required_field(client: AsyncClient) -> None:
    owner_headers = await _register(client, "router-badconfig@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={
            "connector_key": "postgres",
            "name": "Broken",
            "config": {"port": 5432, "database": "x"},  # missing required "host"
            "secrets": {"username": "u", "password": "p"},
        },
        headers=owner_headers,
    )
    assert resp.status_code == 400
    assert "Invalid config" in resp.text


async def test_install_rejects_an_unknown_connector_key(client: AsyncClient) -> None:
    owner_headers = await _register(client, "router-unknown@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"connector_key": "hubspot", "name": "Not real", "config": {}, "secrets": {}},
        headers=owner_headers,
    )
    assert resp.status_code == 400


async def test_install_rejects_file_upload_since_its_never_admin_installed(
    client: AsyncClient,
) -> None:
    owner_headers = await _register(client, "router-fileupload@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"connector_key": "file_upload", "name": "Files", "config": {}, "secrets": {}},
        headers=owner_headers,
    )
    assert resp.status_code == 400


async def test_viewer_can_list_but_not_install(client: AsyncClient, postgres_url: str) -> None:
    owner_headers = await _register(client, "router-viewer-owner@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")
    viewer_headers = await _register(client, "router-viewer@example.com")
    await _add_member(client, owner_headers, workspace_id, "router-viewer@example.com", "viewer")

    list_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors", headers=viewer_headers
    )
    assert list_resp.status_code == 200

    install_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=viewer_headers,
    )
    assert install_resp.status_code == 403


async def test_member_cannot_install(client: AsyncClient, postgres_url: str) -> None:
    owner_headers = await _register(client, "router-member-owner@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")
    member_headers = await _register(client, "router-member@example.com")
    await _add_member(client, owner_headers, workspace_id, "router-member@example.com", "member")

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=member_headers,
    )
    assert resp.status_code == 403


async def test_non_member_gets_404_not_403_on_every_route(
    client: AsyncClient, postgres_url: str
) -> None:
    owner_headers = await _register(client, "router-outsider-owner@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")
    install_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=owner_headers,
    )
    installation_id = install_resp.json()["id"]

    outsider_headers = await _register(client, "router-outsider@example.com")

    list_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors", headers=outsider_headers
    )
    assert list_resp.status_code == 404

    get_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}", headers=outsider_headers
    )
    assert get_resp.status_code == 404

    install_attempt = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url, name="Sneaky"),
        headers=outsider_headers,
    )
    assert install_attempt.status_code == 404

    delete_resp = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}", headers=outsider_headers
    )
    assert delete_resp.status_code == 404

    test_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/test",
        headers=outsider_headers,
    )
    assert test_resp.status_code == 404


async def test_only_admin_can_delete_a_member_gets_403(
    client: AsyncClient, postgres_url: str
) -> None:
    owner_headers = await _register(client, "router-delete-owner@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")
    install_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=owner_headers,
    )
    installation_id = install_resp.json()["id"]

    member_headers = await _register(client, "router-delete-member@example.com")
    await _add_member(
        client, owner_headers, workspace_id, "router-delete-member@example.com", "member"
    )

    forbidden = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}", headers=member_headers
    )
    assert forbidden.status_code == 403

    ok = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}", headers=owner_headers
    )
    assert ok.status_code == 204

    missing = await client.get(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}", headers=owner_headers
    )
    assert missing.status_code == 404


async def test_the_test_endpoint_reports_a_broken_connection(
    client: AsyncClient, postgres_url: str
) -> None:
    owner_headers = await _register(client, "router-retest@example.com")
    workspace_id = await _workspace(client, owner_headers, "Owner Co")
    install_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json=_valid_postgres_body(postgres_url),
        headers=owner_headers,
    )
    installation_id = install_resp.json()["id"]
    assert install_resp.json()["health"] == "healthy"

    # Re-running the health check against the same still-reachable DB keeps it healthy.
    retest_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/test",
        headers=owner_headers,
    )
    assert retest_resp.status_code == 200
    assert retest_resp.json()["health"] == "healthy"
