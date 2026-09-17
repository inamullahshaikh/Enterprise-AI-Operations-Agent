"""The `mcp` connector (docs/system-design.md section 6.4): any remote MCP server becomes an
installation. Streamable HTTP only; `stdio` is never offered, since it would mean running an
admin-supplied command on a Relay host.

**Every tool is a `write` until someone says otherwise.** Relay can't know what a tool it has
never seen does, and a server's own `readOnlyHint` is a claim by the party being guarded against,
so it only reaches the capability tagger as a hint. Discovered tools also declare no
capabilities: sync marks them for review, and the tagger or an admin assigns them.

One MCP session per call, opened through the SSRF guard's `httpx2` client, so every request
(redirects included) is checked and pinned to the address that was checked.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic import BaseModel

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.security.ssrf import check_url, guarded_mcp_http_client


class _Config(BaseModel):
    url: str
    timeout_s: float = 30.0


class McpConnector(Connector):
    key = "mcp"
    display_name = "MCP server"
    auth_type = AuthType.API_KEY

    async def validate_config(self, config: dict[str, Any]) -> None:
        await check_url(_Config.model_validate(config).url)

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        config = _Config.model_validate(ctx.config)
        specs: list[ToolSpec] = []
        async with _session(ctx) as client:
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor)
                specs.extend(
                    ToolSpec(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.input_schema,
                        risk=Risk.WRITE,
                        capabilities=[],
                        idempotent=False,
                        timeout_s=config.timeout_s,
                        read_only_hint=tool.annotations.read_only_hint
                        if tool.annotations
                        else None,
                    )
                    for tool in page.tools
                )
                cursor = page.next_cursor
                if not cursor:
                    return specs

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        try:
            async with _session(ctx) as client:
                result = await client.call_tool(tool_name, args)
        except Exception as exc:  # noqa: BLE001 - SDK and transport failures become tool errors
            return ToolResult(ok=False, error=f"MCP call failed: {_reason(exc)}")
        text = "\n".join(b.text for b in result.content if isinstance(b, TextContent))
        if result.is_error:
            return ToolResult(ok=False, error=text or "The MCP tool reported an error")
        content = result.structured_content if result.structured_content is not None else text
        return ToolResult(ok=True, content=content)

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        try:
            tools = await self.list_tools(ctx)
        except Exception as exc:  # noqa: BLE001 - unreachable or unauthorized is a health result
            return False, f"Cannot reach the MCP server: {_reason(exc)}"
        return True, f"MCP server reachable, {len(tools)} tools"


@asynccontextmanager
async def _session(ctx: ExecutionContext) -> AsyncIterator[Client]:
    config = _Config.model_validate(ctx.config)
    token = ctx.secrets.get("token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx2.Timeout(config.timeout_s)
    async with guarded_mcp_http_client(headers=headers, timeout=timeout) as http:
        transport = streamable_http_client(config.url, http_client=http)
        async with Client(transport, read_timeout_seconds=config.timeout_s) as client:
            yield client


def _reason(exc: BaseException) -> str:
    """The SDK runs on anyio task groups, so the real error usually arrives wrapped in an
    `ExceptionGroup` whose own message says nothing useful."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__
