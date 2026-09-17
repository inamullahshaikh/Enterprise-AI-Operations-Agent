"""The tool registry (docs/system-design.md section 6.7): resolves a plan step's requested
capabilities to actual callable tools.

Installed connectors bind from their `tool_definitions` rows (Phase 6), so a tool discovered at
runtime is callable with no code change: the row carries the `{slug}__{tool_name}` function name
(section 6.6), the spec, and the admin's enable/disable and risk decisions. For each requested
capability only the best-priority installation providing it wins (section 7.2 rule 2), and a row
binds when its installation wins at least one requested capability it provides.

The always-available connectors (`file_upload`, `documents`, `python_sandbox`) have no
installation and no rows, so they still bind live from `list_tools`.

**Retrieval** (section 6.7 step 4). When more installed tools qualify than `limit` and a `query`
is given, only the `limit` nearest by embedding are bound, so one executor call sees a focused
tool set. Always-available tools don't count toward the limit. `approval_gate` passes no query,
so the tools it binds on resume are always a superset of what the model saw: a gated tool can't
be ranked out between interrupt and resume.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.connectors.base import Connector, ExecutionContext, Risk, ToolSpec
from relay_core.connectors.builtin.documents import DocumentsConnector
from relay_core.connectors.builtin.file_upload import FileUploadConnector
from relay_core.connectors.builtin.python_sandbox import PythonSandboxConnector
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.security.crypto import LocalKMS
from relay_core.storage.object_store import ObjectStore
from relay_core.tools.sanitizer import sanitize_schema
from relay_core.tools.sync import installation_secrets


@dataclass(frozen=True)
class BoundTool:
    llm_name: str
    installation_id: uuid.UUID | None
    connector: Connector
    ctx: ExecutionContext
    spec: ToolSpec


class BoundToolSet:
    def __init__(self, tools: list[BoundTool]) -> None:
        self._by_name = {t.llm_name: t for t in tools}

    def __iter__(self) -> Iterator[BoundTool]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def __bool__(self) -> bool:
        return bool(self._by_name)

    def lookup(self, llm_name: str) -> BoundTool | None:
        return self._by_name.get(llm_name)

    def to_gemini_declarations(self) -> list[dict[str, Any]]:
        return [to_gemini_declaration(t) for t in self]


def to_gemini_declaration(bound: BoundTool) -> dict[str, Any]:
    return {
        "name": bound.llm_name,
        "description": f"[{bound.spec.risk.upper()}] {bound.spec.description}"[:1024],
        "parameters": sanitize_schema(bound.spec.input_schema),
    }


class ToolRegistry:
    def __init__(
        self,
        session: AsyncSession,
        object_store: ObjectStore,
        kms: LocalKMS,
        gateway: LLMGateway,
        settings: Settings,
    ) -> None:
        self.session = session
        self.object_store = object_store
        self.kms = kms
        self.gateway = gateway
        self.settings = settings
        self.tool_definitions = ToolDefinitionRepository(session)
        self.credentials = ConnectorCredentialRepository(session)
        self.attachments = AttachmentRepository(session)
        self.collections = CollectionRepository(session)
        self.documents = DocumentRepository(session)
        self.chunks = DocumentChunkRepository(session)
        self.tool_calls = ToolCallRepository(session)
        self.policies = WorkspacePolicyRepository(session)

    async def tools_for_run(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        capabilities: list[str],
        query: str | None = None,
        limit: int = 20,
    ) -> BoundToolSet:
        requested = set(capabilities)
        bound: list[BoundTool] = []

        # Read once per run and handed to every connector, rather than per call: these are
        # governance values a connector enforces itself (`ExecutionContext.policy`), and a
        # policy edit mid-run shouldn't change the rules under a run already in flight.
        policy = await self.policies.get(workspace_id)
        policy_values = {"email_domain_allow": list(policy.email_domain_allow)}

        file_upload_ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="file_upload",
            policy=policy_values,
        )
        file_upload = FileUploadConnector(self.attachments, self.object_store)
        bound.extend(await self._bind(file_upload, "file_upload", None, file_upload_ctx, requested))

        documents_ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="documents",
            policy=policy_values,
        )
        documents_connector = DocumentsConnector(
            self.collections, self.documents, self.chunks, self.gateway, self.settings
        )
        bound.extend(
            await self._bind(documents_connector, "documents", None, documents_ctx, requested)
        )

        sandbox_ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="python_sandbox",
            policy=policy_values,
        )
        sandbox_connector = PythonSandboxConnector(
            self.tool_calls, self.object_store, self.settings
        )
        bound.extend(
            await self._bind(sandbox_connector, "python_sandbox", None, sandbox_ctx, requested)
        )

        rows = await self.tool_definitions.list_bindable(workspace_id, requested)
        # Rows arrive best priority first, so the first installation seen per capability wins.
        winners: dict[str, uuid.UUID] = {}
        for row, installation in rows:
            for capability in requested.intersection(row.capabilities):
                winners.setdefault(capability, installation.id)

        rows = [
            (row, installation)
            for row, installation in rows
            if any(winners[c] == installation.id for c in requested.intersection(row.capabilities))
        ]
        if query and len(rows) > limit:
            rows = await self._nearest(workspace_id, rows, query, limit)

        contexts: dict[uuid.UUID, tuple[Connector, ExecutionContext]] = {}
        for row, installation in rows:
            connector_cls = CONNECTOR_TYPES.get(installation.connector_key)
            if connector_cls is None:
                continue
            if installation.id not in contexts:
                secrets = await installation_secrets(
                    self.session, self.kms, installation, self.settings
                )
                contexts[installation.id] = (
                    connector_cls(),
                    ExecutionContext(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        run_id=run_id,
                        conversation_id=conversation_id,
                        installation_id=str(installation.id),
                        config=installation.config,
                        secrets=secrets,
                        policy=policy_values,
                    ),
                )
            connector, ctx = contexts[installation.id]
            bound.append(
                BoundTool(
                    llm_name=row.llm_name,
                    installation_id=installation.id,
                    connector=connector,
                    ctx=ctx,
                    spec=ToolSpec(
                        name=row.name,
                        description=row.description,
                        input_schema=row.input_schema,
                        risk=Risk(row.risk),
                        capabilities=row.capabilities,
                        idempotent=row.idempotent,
                        timeout_s=row.timeout_s,
                    ),
                )
            )

        return BoundToolSet(bound)

    async def _nearest(
        self,
        workspace_id: uuid.UUID,
        rows: list[tuple[ToolDefinition, ConnectorInstallation]],
        query: str,
        limit: int,
    ) -> list[tuple[ToolDefinition, ConnectorInstallation]]:
        try:
            [vector] = await self.gateway.embed(
                [query], task="RETRIEVAL_QUERY", settings=self.settings
            )
        except Exception:  # noqa: BLE001 - no ranking beats no tools
            return rows
        keep = set(
            await self.tool_definitions.nearest_ids(
                workspace_id, [row.id for row, _ in rows], vector, limit
            )
        )
        return [(row, installation) for row, installation in rows if row.id in keep]

    async def _bind(
        self,
        connector: Connector,
        slug: str,
        installation_id: uuid.UUID | None,
        ctx: ExecutionContext,
        requested: set[str],
    ) -> list[BoundTool]:
        specs = await connector.list_tools(ctx)
        return [
            BoundTool(
                llm_name=f"{slug}__{spec.name}",
                installation_id=installation_id,
                connector=connector,
                ctx=ctx,
                spec=spec,
            )
            for spec in specs
            if requested & set(spec.capabilities)
        ]
