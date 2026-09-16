"""The `python_sandbox` connector (docs/system-design.md section 10.7): calls the sandbox
service (`sandbox/`) over HTTP to run analysis code in an isolated, ephemeral container. Always
available like `file_upload`/`documents` — `settings.sandbox_url` is shared workspace infra, not
a per-installation credential, so there's no `connector_installations` row here either.

**`ref://tool_call/<id>` handles.** `inputs` values may reference an earlier tool call's full
output by id instead of inlining it, so a step that already ran `postgres__run_sql` can hand its
result straight to analysis code without the model ever having to copy large data through its
own context (docs/system-design.md section 8.6). This works today without a separate
truncate-large-outputs-into-a-preview mechanism: `relay_core.tools.executor.ToolExecutor`
already stores every tool call's full, untruncated `output` on `tool_calls` (for audit), so
resolving a ref here is just reading that row back.
"""

import base64
import uuid
from typing import Any

import httpx

from relay_core.config import Settings
from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.storage.object_store import ObjectStore

_CAPABILITIES = ["code.execute"]
_REF_PREFIX = "ref://tool_call/"
_ARTIFACT_CONTENT_TYPES = {
    ".png": "image/png",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
}


class PythonSandboxConnector(Connector):
    key = "python_sandbox"
    display_name = "Python sandbox"
    auth_type = AuthType.NONE

    def __init__(
        self,
        tool_calls: ToolCallRepository,
        object_store: ObjectStore,
        settings: Settings,
    ) -> None:
        self.tool_calls = tool_calls
        self.object_store = object_store
        self.settings = settings

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_python",
                description=(
                    "Run Python (pandas/numpy/scipy/matplotlib/scikit-learn available) for "
                    "analysis or charts, in an isolated sandbox with no network or database "
                    "access. Read inputs from the `inputs` dict (available as a global variable "
                    "named `inputs`); an input value may be a ref://tool_call/<id> handle "
                    "(from an earlier tool result) instead of inline data. Write any output "
                    "files (e.g. a chart PNG) to the current working directory."
                ),
                input_schema={
                    "type": "object",
                    "required": ["code"],
                    "properties": {
                        "code": {"type": "string"},
                        "inputs": {"type": "object", "additionalProperties": True},
                    },
                },
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
                # Re-running arbitrary code automatically on a transient network blip could
                # double a real side-effect-free-but-slow computation; not worth it given the
                # sandbox itself already enforces a hard timeout (section 10.7).
                idempotent=False,
                timeout_s=self._client_timeout_s(),
            )
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        if tool_name != "run_python":
            return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")
        code = args.get("code")
        if not code or not isinstance(code, str):
            return ToolResult(ok=False, error="'code' is required")

        resolved_inputs = await self._resolve_inputs(ctx, args.get("inputs") or {})

        try:
            async with httpx.AsyncClient(timeout=self._client_timeout_s()) as client:
                response = await client.post(
                    f"{self.settings.sandbox_url}/run",
                    json={
                        "code": code,
                        "inputs": resolved_inputs,
                        "timeout_s": self.settings.sandbox_timeout_s,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Sandbox request failed: {exc}")

        body = response.json()
        artifacts = await self._upload_artifacts(ctx, body.get("files") or {})
        return ToolResult(
            ok=bool(body.get("ok")),
            content={"stdout": body.get("stdout", ""), "stderr": body.get("stderr", "")},
            error=body.get("error"),
            artifacts=artifacts,
            truncated=bool(body.get("truncated")),
            meta={"exit_code": body.get("exit_code"), "timed_out": body.get("timed_out")},
        )

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.settings.sandbox_url}/healthz")
        except httpx.HTTPError as exc:
            return False, f"Could not connect: {exc}"
        if response.status_code == 200:
            return True, "Connected"
        return False, f"Unexpected status {response.status_code}"

    def _client_timeout_s(self) -> float:
        # Headroom over the sandbox's own hard timeout so this connector's HTTP call doesn't
        # give up before the sandbox service itself has finished killing a runaway container.
        return self.settings.sandbox_timeout_s + 15.0

    async def _resolve_inputs(
        self, ctx: ExecutionContext, raw_inputs: dict[str, Any]
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for name, value in raw_inputs.items():
            if isinstance(value, str) and value.startswith(_REF_PREFIX):
                resolved[name] = await self._resolve_ref(ctx, value)
            else:
                resolved[name] = value
        return resolved

    async def _resolve_ref(self, ctx: ExecutionContext, ref: str) -> Any:
        raw_id = ref.removeprefix(_REF_PREFIX)
        try:
            call_id = uuid.UUID(raw_id)
        except ValueError:
            return None
        call = await self.tool_calls.get(ctx.workspace_id, call_id)
        return call.output if call is not None else None

    async def _upload_artifacts(self, ctx: ExecutionContext, files: dict[str, str]) -> list[str]:
        keys: list[str] = []
        for filename, encoded in files.items():
            data = base64.b64decode(encoded)
            suffix = filename[filename.rfind(".") :] if "." in filename else ""
            content_type = _ARTIFACT_CONTENT_TYPES.get(suffix, "application/octet-stream")
            key = f"sandbox-artifacts/{ctx.workspace_id}/{ctx.run_id}/{uuid.uuid4()}/{filename}"
            await self.object_store.put_bytes(key, data, content_type=content_type)
            keys.append(key)
        return keys
