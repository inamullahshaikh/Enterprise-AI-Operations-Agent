"""Coverage for `relay_core.connectors.builtin.python_sandbox.PythonSandboxConnector`
(docs/system-design.md section 10.7): the HTTP boundary to the sandbox service (mocked here with
`respx` — the service's own container-orchestration logic is `sandbox/runner.py`, a separate
process this backend test suite doesn't run), `ref://tool_call/<id>` input resolution against a
real `tool_calls` row, and output-file-to-artifact upload.
"""

import base64
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.python_sandbox import PythonSandboxConnector
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository

pytestmark = pytest.mark.asyncio


class _FakeObjectStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts.append((key, data, content_type))

    async def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError


async def _register_run(client: AsyncClient, db_session: AsyncSession, email: str):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    user_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])
    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    workspace_id = uuid.UUID(ws_resp.json()["id"])
    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    conversation_id = uuid.UUID(conv_resp.json()["id"])
    run = await AgentRunRepository(db_session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        trigger_message_id=None,
    )
    ctx = ExecutionContext(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=run.id,
        conversation_id=conversation_id,
        installation_id="python_sandbox",
    )
    return ctx


def _connector(db_session: AsyncSession, object_store, test_settings) -> PythonSandboxConnector:
    return PythonSandboxConnector(ToolCallRepository(db_session), object_store, test_settings)


async def test_run_python_calls_the_sandbox_service_and_returns_the_result(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-run@example.com")
    connector = _connector(db_session, _FakeObjectStore(), test_settings)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{test_settings.sandbox_url}/run").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "stdout": "42\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "error": None,
                    "files": {},
                    "truncated": False,
                },
            )
        )
        result = await connector.call_tool(ctx, "run_python", {"code": "print(42)"})

    assert result.ok is True
    assert result.content == {"stdout": "42\n", "stderr": ""}
    assert result.artifacts == []


async def test_run_python_uploads_output_files_as_artifacts(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-artifacts@example.com")
    object_store = _FakeObjectStore()
    connector = _connector(db_session, object_store, test_settings)
    png_bytes = b"\x89PNG\r\n\x1a\nfake"

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{test_settings.sandbox_url}/run").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "error": None,
                    "files": {"chart.png": base64.b64encode(png_bytes).decode()},
                    "truncated": False,
                },
            )
        )
        result = await connector.call_tool(
            ctx, "run_python", {"code": "plt.savefig('chart.png')"}
        )

    assert len(result.artifacts) == 1
    assert result.artifacts[0].endswith("chart.png")
    assert len(object_store.puts) == 1
    key, data, content_type = object_store.puts[0]
    assert data == png_bytes
    assert content_type == "image/png"
    assert key == result.artifacts[0]


async def test_run_python_resolves_a_ref_tool_call_handle(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-ref@example.com")
    tool_calls = ToolCallRepository(db_session)
    earlier = await tool_calls.start(
        workspace_id=ctx.workspace_id,
        run_id=ctx.run_id,
        plan_step_id="s1",
        installation_id=None,
        llm_name="sales-db__run_sql",
        arguments={"sql": "SELECT 1"},
        risk="read",
    )
    await tool_calls.finish(
        ctx.workspace_id,
        earlier.id,
        ok=True,
        output={"ok": True, "content": [{"account": "Acme", "mrr": 1200}]},
        error=None,
        latency_ms=5,
    )

    connector = PythonSandboxConnector(tool_calls, _FakeObjectStore(), test_settings)
    captured_body = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        import json

        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "error": None,
                "files": {},
                "truncated": False,
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{test_settings.sandbox_url}/run").mock(side_effect=_capture)
        await connector.call_tool(
            ctx,
            "run_python",
            {"code": "print(inputs['data'])", "inputs": {"data": f"ref://tool_call/{earlier.id}"}},
        )

    assert captured_body["inputs"]["data"] == {
        "ok": True,
        "content": [{"account": "Acme", "mrr": 1200}],
    }


async def test_call_tool_requires_code(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-nocode@example.com")
    connector = _connector(db_session, _FakeObjectStore(), test_settings)
    result = await connector.call_tool(ctx, "run_python", {})
    assert result.ok is False
    assert "'code' is required" in (result.error or "")


async def test_call_tool_unknown_tool_name(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-unknown@example.com")
    connector = _connector(db_session, _FakeObjectStore(), test_settings)
    result = await connector.call_tool(ctx, "delete_everything", {})
    assert result.ok is False
    assert "Unknown tool" in (result.error or "")


async def test_health_check_reports_unhealthy_on_connection_error(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-health@example.com")
    connector = _connector(db_session, _FakeObjectStore(), test_settings)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{test_settings.sandbox_url}/healthz").mock(
            side_effect=httpx.ConnectError("refused")
        )
        healthy, message = await connector.health_check(ctx)

    assert healthy is False
    assert "Could not connect" in message


async def test_health_check_succeeds(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    ctx = await _register_run(client, db_session, "sandbox-health-ok@example.com")
    connector = _connector(db_session, _FakeObjectStore(), test_settings)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{test_settings.sandbox_url}/healthz").mock(return_value=httpx.Response(200))
        healthy, message = await connector.health_check(ctx)

    assert healthy is True
    assert message == "Connected"
