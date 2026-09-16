"""The connector interface (docs/system-design.md section 6.2). Every built-in, MCP, and
OpenAPI connector implements this same `Connector` ABC and is normalized into the same
`ToolSpec`/`ToolResult` shapes, so the tool registry and executor never know or care where a
tool came from (section 6.1). MCP and OpenAPI adapters are Phase 6 additions; only the ABC
itself (needed by the two Phase 3 built-ins) lands now.
"""

import uuid
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class AuthType(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BASIC = "basic"
    OAUTH2 = "oauth2"
    CONNECTION_STRING = "connection_string"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risk: Risk = Risk.READ
    capabilities: list[str] = Field(default_factory=list)
    idempotent: bool = True
    timeout_s: float = 30.0


class ToolResult(BaseModel):
    ok: bool
    content: Any = None
    error: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    truncated: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutionContext(BaseModel):
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    run_id: uuid.UUID
    # Not in the original design sketch (section 6.2): `file_upload` needs to know which
    # conversation's attachments to read, and every run happens within exactly one
    # conversation, so this is cheap to thread through rather than have every connector
    # that needs it look up `agent_runs.conversation_id` from `run_id` itself.
    conversation_id: uuid.UUID
    installation_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class Connector(ABC):
    key: ClassVar[str]
    display_name: ClassVar[str]
    auth_type: ClassVar[AuthType]

    @abstractmethod
    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]: ...

    @abstractmethod
    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult: ...

    @abstractmethod
    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]: ...

    async def on_install(self, ctx: ExecutionContext) -> None:  # noqa: B027 - optional hook, not abstract
        """Optional: validate config, create webhooks, warm caches."""

    async def on_uninstall(self, ctx: ExecutionContext) -> None:  # noqa: B027 - optional hook, not abstract
        """Optional: revoke tokens, remove webhooks."""
