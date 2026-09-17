"""The `openapi` connector (docs/system-design.md section 6.5 step 5): the operations an admin
picked from a spec preview (`relay_core.connectors.openapi_spec`) become tools that call the
admin's base URL.

The selected operations live in the installation's config, so `list_tools` rebuilds specs from
there: nothing to store elsewhere, nothing to re-parse, and nothing that changes upstream.

**The host is fixed.** Every request goes to `base_url + operation.path` through the SSRF guard.
Path arguments are percent-encoded with no safe characters, so a value can't add a `/` or a
`..` segment, and nothing in the arguments can change the host. Risk comes from the HTTP method,
recomputed on the server; a `risk` sent in the config is ignored.
"""

import base64
import json
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    ToolResult,
    ToolSpec,
)
from relay_core.connectors.openapi_spec import Operation
from relay_core.security.ssrf import check_url, guarded_client, read_capped

_MAX_ERROR_BODY = 2000


class _Auth(BaseModel):
    type: Literal["none", "bearer", "api_key_header", "basic"] = "none"
    header_name: str | None = None


class _Config(BaseModel):
    base_url: str
    auth: _Auth = _Auth()
    operations: list[Operation] = Field(max_length=50)


class OpenApiConnector(Connector):
    key = "openapi"
    display_name = "OpenAPI"
    auth_type = AuthType.API_KEY

    async def validate_config(self, config: dict[str, Any]) -> None:
        cfg = _Config.model_validate(config)
        await check_url(cfg.base_url)
        if cfg.auth.type == "api_key_header" and not cfg.auth.header_name:
            raise ValueError("auth.header_name is required for api_key_header auth")
        names = [op.name for op in cfg.operations]
        if len(set(names)) != len(names):
            raise ValueError("Operation names must be unique")
        for op in cfg.operations:
            if not op.path.startswith("/") or op.path.startswith("//") or "://" in op.path:
                raise ValueError(f"Operation {op.name!r}: path must be relative, like /users")

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=op.name,
                description=op.description or f"{op.method.upper()} {op.path}",
                input_schema=op.input_schema,
                risk=op.risk,
                capabilities=[],
                idempotent=op.method in ("get", "head"),
            )
            for op in _Config.model_validate(ctx.config).operations
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        cfg = _Config.model_validate(ctx.config)
        op = next((o for o in cfg.operations if o.name == tool_name), None)
        if op is None:
            return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

        path, query = op.path, {}
        for param in op.params:
            if param.name not in args:
                continue
            value = str(args[param.name])
            if param.location == "query":
                query[param.name] = value
            elif value in (".", ".."):
                return ToolResult(ok=False, error=f"Invalid value for path parameter {param.name}")
            else:
                path = path.replace(f"{{{param.name}}}", quote(value, safe=""))

        headers = _auth_headers(cfg.auth, ctx.secrets)
        if ctx.idempotency_key:
            headers["Idempotency-Key"] = ctx.idempotency_key
        try:
            async with (
                guarded_client() as http,
                http.stream(
                    op.method.upper(),
                    cfg.base_url.rstrip("/") + path,
                    params=query,
                    json=args.get("body") if op.has_body else None,
                    headers=headers,
                ) as resp,
            ):
                raw = await read_capped(resp)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Request failed: {exc}")

        text = raw.decode("utf-8", errors="replace")
        if resp.status_code >= 400:
            return ToolResult(ok=False, error=f"HTTP {resp.status_code}: {text[:_MAX_ERROR_BODY]}")
        if "json" in resp.headers.get("content-type", ""):
            try:
                return ToolResult(ok=True, content=json.loads(text))
            except ValueError:
                pass
        return ToolResult(ok=True, content=text)

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        cfg = _Config.model_validate(ctx.config)
        try:
            async with guarded_client() as http:
                resp = await http.get(cfg.base_url, headers=_auth_headers(cfg.auth, ctx.secrets))
        except httpx.HTTPError as exc:
            return False, f"Cannot reach {cfg.base_url}: {exc}"
        return True, f"Reachable (HTTP {resp.status_code})"


def _auth_headers(auth: _Auth, secrets: dict[str, str]) -> dict[str, str]:
    if auth.type == "bearer":
        return {"Authorization": f"Bearer {secrets.get('token', '')}"}
    if auth.type == "api_key_header" and auth.header_name:
        return {auth.header_name: secrets.get("api_key", "")}
    if auth.type == "basic":
        pair = f"{secrets.get('username', '')}:{secrets.get('password', '')}".encode()
        return {"Authorization": f"Basic {base64.b64encode(pair).decode()}"}
    return {}
