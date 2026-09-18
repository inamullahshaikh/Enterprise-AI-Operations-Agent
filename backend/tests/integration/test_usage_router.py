"""Phase 8 A2: `GET /workspaces/{ws}/usage` aggregates `llm_calls` and `tool_calls` for one
workspace and one window (docs/system-design.md section 15.5)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.llm import LLMCall
from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.users import UserRepository

pytestmark = pytest.mark.asyncio

_WINDOW = {"from": "2026-03-01T00:00:00Z", "to": "2026-03-10T00:00:00Z"}


async def _register(client: AsyncClient, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": email},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, headers: dict) -> uuid.UUID:
    resp = await client.post("/api/v1/workspaces", json={"name": "Usage Co"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _llm(ws: uuid.UUID, model: str, cost: str, day: int) -> LLMCall:
    return LLMCall(
        workspace_id=ws,
        node="plan",
        model=model,
        input_tokens=100,
        output_tokens=10,
        cost_usd=Decimal(cost),
        latency_ms=5,
        status="ok",
        created_at=datetime(2026, 3, day, 12, tzinfo=UTC),
    )


async def _seed(db_session: AsyncSession, ws: uuid.UUID, email: str) -> None:
    db_session.add_all(
        [
            _llm(ws, "gemini-pro", "0.50", 1),
            _llm(ws, "gemini-flash", "0.10", 1),
            _llm(ws, "gemini-flash", "0.20", 2),
            _llm(ws, "gemini-flash", "9.00", 28),  # outside _WINDOW
        ]
    )
    user = await UserRepository(db_session).get_by_email(email)
    assert user is not None
    conversation = await ConversationRepository(db_session).create(workspace_id=ws, user_id=user.id)
    run = await AgentRunRepository(db_session).create(
        workspace_id=ws, conversation_id=conversation.id, user_id=user.id, trigger_message_id=None
    )
    for status in ("succeeded", "succeeded", "failed"):
        db_session.add(
            ToolCall(
                workspace_id=ws,
                run_id=run.id,
                plan_step_id="s1",
                llm_name="upload__read",
                arguments={},
                risk="read",
                status=status,
                created_at=datetime(2026, 3, 1, tzinfo=UTC),
            )
        )
    await db_session.flush()


async def test_groups_by_model_and_day_within_the_window(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "usage-owner@example.com")
    ws = await _workspace(client, headers)
    await _seed(db_session, ws, "usage-owner@example.com")
    url = f"/api/v1/workspaces/{ws}/usage"

    resp = await client.get(url, params={**_WINDOW, "group_by": "model"}, headers=headers)
    assert resp.status_code == 200, resp.text
    by_model = {r["group_key"]: r for r in resp.json()["llm_calls"]}
    assert set(by_model) == {"gemini-flash", "gemini-pro"}
    assert by_model["gemini-flash"]["llm_calls"] == 2
    assert Decimal(by_model["gemini-flash"]["cost_usd"]) == Decimal("0.30")
    assert by_model["gemini-pro"]["input_tokens"] == 100

    resp = await client.get(url, params={**_WINDOW, "group_by": "day"}, headers=headers)
    by_day = {r["group_key"]: r["llm_calls"] for r in resp.json()["llm_calls"]}
    assert by_day == {"2026-03-01": 2, "2026-03-02": 1}

    tools = {(r["connector_key"], r["status"]): r["calls"] for r in resp.json()["tool_calls"]}
    assert tools == {("none", "failed"): 1, ("none", "succeeded"): 2}


async def test_member_gets_403_and_non_member_404(client: AsyncClient) -> None:
    owner = await _register(client, "usage-rbac-owner@example.com")
    ws = await _workspace(client, owner)
    member = await _register(client, "usage-rbac-member@example.com")
    await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": "usage-rbac-member@example.com", "role": "member"},
        headers=owner,
    )
    outsider = await _register(client, "usage-rbac-outsider@example.com")

    url = f"/api/v1/workspaces/{ws}/usage"
    assert (await client.get(url, headers=member)).status_code == 403
    assert (await client.get(url, headers=outsider)).status_code == 404


async def test_another_workspaces_spend_never_appears(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _register(client, "usage-a@example.com")
    ws_a = await _workspace(client, headers)
    ws_b = await _workspace(client, headers)
    await _seed(db_session, ws_a, "usage-a@example.com")

    resp = await client.get(f"/api/v1/workspaces/{ws_b}/usage", params=_WINDOW, headers=headers)
    assert resp.json() == {"llm_calls": [], "tool_calls": []}
