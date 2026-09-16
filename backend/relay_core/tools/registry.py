"""The tool registry (docs/system-design.md section 6.7): resolves a plan step's requested
capabilities to actual callable tools, normalizing built-ins into the `{slug}__{tool_name}`
naming scheme every Gemini function declaration needs (section 6.6). No tool-count `limit` or
vector-ranking `query` yet — both are Phase 6 once embedding-based tool retrieval and large
tool counts (MCP/OpenAPI) actually make that necessary (docs/adr/0009); Phase 3 has two
connector types with a handful of tools each.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.connectors.base import Connector, ExecutionContext, ToolSpec
from relay_core.connectors.builtin.documents import DocumentsConnector
from relay_core.connectors.builtin.file_upload import FileUploadConnector
from relay_core.connectors.builtin.python_sandbox import PythonSandboxConnector
from relay_core.connectors.manifest import load_manifests
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.security.credential_codec import decrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.storage.object_store import ObjectStore
from relay_core.tools.sanitizer import sanitize_schema

# Always-available connectors (no installation row): bound on every run regardless of what's
# requested from them (matching relay_api.routers.connectors._ALWAYS_AVAILABLE_KEYS), so they
# never compete with an admin-installed connector of the same manifest key.
_ALWAYS_AVAILABLE_KEYS = frozenset({"file_upload", "documents", "python_sandbox"})


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
        self.installations = ConnectorInstallationRepository(session)
        self.credentials = ConnectorCredentialRepository(session)
        self.attachments = AttachmentRepository(session)
        self.collections = CollectionRepository(session)
        self.documents = DocumentRepository(session)
        self.chunks = DocumentChunkRepository(session)
        self.tool_calls = ToolCallRepository(session)

    async def tools_for_run(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        capabilities: list[str],
    ) -> BoundToolSet:
        requested = set(capabilities)
        manifests = load_manifests()
        bound: list[BoundTool] = []

        file_upload_ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="file_upload",
        )
        file_upload = FileUploadConnector(self.attachments, self.object_store)
        bound.extend(await self._bind(file_upload, "file_upload", None, file_upload_ctx, requested))

        documents_ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="documents",
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
        )
        sandbox_connector = PythonSandboxConnector(
            self.tool_calls, self.object_store, self.settings
        )
        bound.extend(
            await self._bind(sandbox_connector, "python_sandbox", None, sandbox_ctx, requested)
        )

        provider_keys = {
            key
            for key, manifest in manifests.items()
            if key not in _ALWAYS_AVAILABLE_KEYS and requested & set(manifest.provides_capabilities)
        }
        for installation in await self.installations.list_active_by_connector_keys(
            workspace_id, provider_keys
        ):
            connector_cls = CONNECTOR_TYPES.get(installation.connector_key)
            if connector_cls is None:
                continue
            secrets: dict[str, str] = {}
            credential = await self.credentials.get(workspace_id, installation.id)
            if credential is not None:
                secrets = decrypt_secrets(self.kms, credential)
            ctx = ExecutionContext(
                workspace_id=workspace_id,
                user_id=user_id,
                run_id=run_id,
                conversation_id=conversation_id,
                installation_id=str(installation.id),
                config=installation.config,
                secrets=secrets,
            )
            bound.extend(
                await self._bind(
                    connector_cls(), installation.slug, installation.id, ctx, requested
                )
            )

        return BoundToolSet(bound)

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
