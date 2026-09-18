"""Phase 8 A1: `audit_logs` rows from the admin actions that write them, the admin-only route,
secret scrubbing of `details`, and keyset pagination (docs/system-design.md sections 14.3, 15.5)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_resume_dispatcher
from relay_api.main import app
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.audit import AuditLogRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.security.scrub import REDACTED

pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": email},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, headers: dict) -> str:
    resp = await client.post("/api/v1/workspaces", json={"name": "Audit Co"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _logs(client: AsyncClient, headers: dict, ws: str, **params: object) -> list[dict]:
    resp = await client.get(f"/api/v1/workspaces/{ws}/audit-logs", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_admin_actions_each_leave_one_row_with_the_actor(
    client: AsyncClient, db_session: AsyncSession, postgres_url: str
) -> None:
    headers = await _register(client, "audit-owner@example.com")
    ws = await _workspace(client, headers)
    owner = await UserRepository(db_session).get_by_email("audit-owner@example.com")
    assert owner is not None

    url = make_url(postgres_url)
    resp = await client.post(
        f"/api/v1/workspaces/{ws}/connectors",
        json={
            "connector_key": "postgres",
            "name": "Demo DB",
            "config": {"host": url.host, "port": url.port, "database": url.database},
            "secrets": {"username": url.username, "password": url.password},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    await _register(client, "audit-member@example.com")
    resp = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": "audit-member@example.com", "role": "member"},
        headers=headers,
    )
    member_id = resp.json()["user_id"]
    resp = await client.patch(
        f"/api/v1/workspaces/{ws}/members/{member_id}", json={"role": "admin"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    conversation = await ConversationRepository(db_session).create(
        workspace_id=uuid.UUID(ws), user_id=owner.id
    )
    run = await AgentRunRepository(db_session).create(
        workspace_id=uuid.UUID(ws),
        conversation_id=conversation.id,
        user_id=owner.id,
        trigger_message_id=None,
    )
    approval = await ApprovalRepository(db_session).create(
        workspace_id=uuid.UUID(ws),
        run_id=run.id,
        tool_call_ids=[],
        summary="Send an email",
        proposed_args=[],
        requested_by=owner.id,
    )

    async def _no_resume(*_: object) -> None:
        return None

    app.dependency_overrides[get_resume_dispatcher] = lambda: _no_resume
    try:
        resp = await client.post(
            f"/api/v1/workspaces/{ws}/approvals/{approval.id}/decision",
            json={"action": "reject", "reason": "not now"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.pop(get_resume_dispatcher, None)

    rows = await _logs(client, headers, ws)
    by_action: dict[str, list[dict]] = {}
    for row in rows:
        by_action.setdefault(row["action"], []).append(row)
    for action in (
        "connector.installed",
        "credentials.written",
        "member.role_changed",
        "approval.decided",
    ):
        assert len(by_action[action]) == 1, (action, rows)
        assert by_action[action][0]["actor_user_id"] == str(owner.id)
        assert by_action[action][0]["actor_type"] == "user"
    assert by_action["member.role_changed"][0]["details"] == {"from": "member", "to": "admin"}
    assert by_action["approval.decided"][0]["run_id"] == str(run.id)

    only = await _logs(client, headers, ws, action="approval.decided")
    assert [r["action"] for r in only] == ["approval.decided"]


async def test_member_gets_403_and_non_member_404(client: AsyncClient) -> None:
    owner = await _register(client, "audit-rbac-owner@example.com")
    ws = await _workspace(client, owner)
    member = await _register(client, "audit-rbac-member@example.com")
    await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": "audit-rbac-member@example.com", "role": "member"},
        headers=owner,
    )
    outsider = await _register(client, "audit-rbac-outsider@example.com")

    url = f"/api/v1/workspaces/{ws}/audit-logs"
    assert (await client.get(url, headers=member)).status_code == 403
    assert (await client.get(url, headers=outsider)).status_code == 404


async def test_secrets_in_details_are_stored_redacted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "audit-scrub@example.com")
    ws = uuid.UUID(await _workspace(client, headers))
    row = await AuditLogRepository(db_session).record(
        ws,
        actor_type="system",
        actor_user_id=None,
        action="test.secret",
        target_type="test",
        details={
            "nested": {"Authorization": "Bearer abc", "client_secret": "s3cret"},
            "note": "key is sk-abcdefghijklmnopqrstuvwx",
            "count": 3,
        },
    )
    await db_session.refresh(row)
    assert row.details == {
        "nested": {"Authorization": REDACTED, "client_secret": REDACTED},
        "note": f"key is {REDACTED}",
        "count": 3,
    }


async def test_keyset_pagination_returns_each_row_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "audit-page@example.com")
    ws = await _workspace(client, headers)
    repo = AuditLogRepository(db_session)
    # One transaction, so every row shares a created_at: the id tiebreak is what's under test.
    for i in range(5):
        await repo.record(
            uuid.UUID(ws),
            actor_type="system",
            actor_user_id=None,
            action="test.page",
            target_type="test",
            target_id=i,
        )

    first = await _logs(client, headers, ws, action="test.page", limit=3)
    second = await _logs(client, headers, ws, action="test.page", limit=3, before=first[-1]["id"])
    ids = [r["id"] for r in first + second]
    assert len(first) == 3 and len(second) == 2
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 5


async def test_policy_update_is_audited_with_before_and_after(client: AsyncClient) -> None:
    headers = await _register(client, "audit-policy@example.com")
    ws = await _workspace(client, headers)

    resp = await client.patch(
        f"/api/v1/workspaces/{ws}/policy",
        json={
            "memory_enabled": False,
            "run_budget": {"max_llm_calls": 5},
            "approval_rules": {
                "default_write": "always",
                "overrides": [{"tool": "x__create_draft", "rule": "never"}],
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memory_enabled"] is False
    assert body["run_budget"]["max_llm_calls"] == 5
    assert body["run_budget"]["max_steps"] == 10  # untouched keys survive the merge

    [row] = await _logs(client, headers, ws, action="policy.updated")
    assert row["details"]["from"]["memory_enabled"] is True
    assert row["details"]["to"]["memory_enabled"] is False
    assert row["ip"]  # bound by the API middleware, not passed by the route


async def test_policy_patch_refuses_invalid_rules(client: AsyncClient) -> None:
    headers = await _register(client, "audit-policy-bad@example.com")
    ws = await _workspace(client, headers)
    url = f"/api/v1/workspaces/{ws}/policy"
    bad = {"approval_rules": {"default_write": "sometimes"}}
    assert (await client.patch(url, json=bad, headers=headers)).status_code == 422
    unknown = {"run_budget": {"max_bananas": 1}}
    assert (await client.patch(url, json=unknown, headers=headers)).status_code == 422
