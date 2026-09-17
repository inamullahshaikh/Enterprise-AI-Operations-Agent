"""Every workspace must have a `workspace_policies` row (docs/system-design.md sections 13.1,
14.3). `WorkspacePolicyRepository.get` treats that as an invariant and raises instead of
inventing defaults, so this covers the provisioning path that upholds it — if creating a
workspace ever stops writing the policy row, the first write tool call in a new workspace would
fail its approval check rather than fall back to something permissive.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.policy import ApprovalRules

pytestmark = pytest.mark.asyncio


async def _register_and_create_workspace(client: AsyncClient, email: str) -> uuid.UUID:
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register_resp.status_code == 201, register_resp.text
    headers = {"Authorization": f"Bearer {register_resp.json()['access_token']}"}
    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    assert ws_resp.status_code == 201, ws_resp.text
    return uuid.UUID(ws_resp.json()["id"])


async def test_creating_a_workspace_provisions_its_policy_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id = await _register_and_create_workspace(client, "policy-new@example.com")

    policy = await WorkspacePolicyRepository(db_session).get(workspace_id)

    assert policy.workspace_id == workspace_id
    assert policy.email_domain_allow == []
    assert policy.allow_web_grounding is False
    assert policy.pii_redaction is True
    assert policy.run_budget["max_tool_calls"] == 40


async def test_default_policy_requires_approval_for_every_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Section 13.1's "Default policy: all write and destructive calls need approval" — asserted
    against a row that actually went through the database's own column defaults, not just the
    Python-side ones."""
    workspace_id = await _register_and_create_workspace(client, "policy-default@example.com")

    rules = await WorkspacePolicyRepository(db_session).approval_rules(workspace_id)

    assert rules == ApprovalRules(default_write="always", overrides=[])


async def test_missing_policy_row_raises_rather_than_defaulting(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="workspace_policies row missing"):
        await WorkspacePolicyRepository(db_session).get(uuid.uuid4())
