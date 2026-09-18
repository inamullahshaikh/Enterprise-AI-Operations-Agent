"""Phase 8 C3: with `pii_redaction` on, the model sees placeholders and the connector and the user
see real values (docs/system-design.md section 20.4)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.nodes.validate_final import FinalVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from tests.integration.fake_tools import (
    READ,
    WRITE,
    FakeConnector,
    fake_connector,  # noqa: F401 - fixture
    function_call,
    preamble,
)
from tests.integration.scripted_model import (
    install_dispatcher,
    register_workspace_and_conversation,
    scripted_gateway,
    send_and_wait,
    text_response,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def _run(client, db_session, redis_client, test_settings, email, *, pii: bool, draft_to):
    headers, ws, conv = await register_workspace_and_conversation(client, email)
    rules = {"default_write": "always", "overrides": [{"tool": WRITE, "rule": "never"}]}
    resp = await client.patch(
        f"/api/v1/workspaces/{ws}/policy",
        json={"approval_rules": rules, "pii_redaction": pii},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    responses = [
        *preamble("Reply to Dana"),
        function_call(READ, {}),
        function_call(WRITE, {"to": draft_to}),
        text_response(f"Drafted a reply to {draft_to}."),
        text_response(StepVerdict(status="pass", reason="ok").model_dump_json()),
        text_response(FinalVerdict(status="pass", reason="ok").model_dump_json()),
    ]
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=responses,
        stream_texts=[f"I drafted a reply to {draft_to}."],
    )
    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        run = await send_and_wait(client, headers, ws, conv, "Reply to Dana")
    finally:
        teardown()
    messages = await client.get(
        f"/api/v1/workspaces/{ws}/conversations/{conv}/messages", headers=headers
    )
    return run, models, messages.json()[-1]["content"]


async def test_the_model_sees_placeholders_and_the_connector_and_user_see_real_values(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    fake_connector: FakeConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeConnector, "untrusted_source", False)
    fake_connector.inbox = {"from": "dana@acme.test", "phone": "+1 415 555 0100"}
    run, models, answer = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "pii-on@example.com",
        pii=True,
        draft_to="<email:1>",
    )

    assert run["status"] == "completed", run
    seen = str(models.contents)
    assert "<email:1>" in seen and "<phone:1>" in seen
    assert "dana@acme.test" not in seen and "555 0100" not in seen
    assert fake_connector.drafts == [{"to": "dana@acme.test"}]
    assert answer == "I drafted a reply to dana@acme.test."


async def test_with_redaction_off_nothing_changes(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    fake_connector: FakeConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeConnector, "untrusted_source", False)
    fake_connector.inbox = {"from": "dana@acme.test"}
    _, models, _ = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "pii-off@example.com",
        pii=False,
        draft_to="dana@acme.test",
    )
    assert "dana@acme.test" in str(models.contents)
    assert "<email:" not in str(models.contents)
    assert fake_connector.drafts == [{"to": "dana@acme.test"}]
