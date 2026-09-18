"""The `documents` connector (docs/system-design.md section 10.2): search, read, and list the
workspace's ingested knowledge base. Always available, like `file_upload` (section 10.8) — a
workspace's collections/documents aren't an external system to install credentials for, they're
Relay's own storage, so there's no `connector_installations` row for this connector either.
"""

import uuid
from typing import Any

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.rag.retrieval import hybrid_search

_CAPABILITIES = ["knowledge.search"]
_DEFAULT_TOP_K = 6
_MAX_GET_DOCUMENT_CHARS = 20_000


class DocumentsConnector(Connector):
    key = "documents"
    untrusted_source = False
    display_name = "Documents"
    auth_type = AuthType.NONE

    def __init__(
        self,
        collections: CollectionRepository,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
        gateway: LLMGateway,
        settings: Any,
    ) -> None:
        self.collections = collections
        self.documents = documents
        self.chunks = chunks
        self.gateway = gateway
        self.settings = settings

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search_documents",
                description=(
                    "Search the workspace's knowledge base (policies, playbooks, and other "
                    "ingested documents) and return the most relevant passages with citations."
                ),
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "collection": {
                            "type": "string",
                            "description": "Optional collection name to restrict the search to.",
                        },
                        "top_k": {"type": "integer", "default": _DEFAULT_TOP_K},
                    },
                },
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
            ToolSpec(
                name="get_document",
                description=(
                    "Read a document's full text by its document_id (from search_documents)."
                ),
                input_schema={
                    "type": "object",
                    "required": ["document_id"],
                    "properties": {
                        "document_id": {"type": "string"},
                        "page_start": {"type": "integer"},
                        "page_end": {"type": "integer"},
                    },
                },
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
            ToolSpec(
                name="list_collections",
                description="List the knowledge base's collections.",
                input_schema={"type": "object", "properties": {}},
                risk=Risk.READ,
                capabilities=_CAPABILITIES,
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        if tool_name == "search_documents":
            return await self._search_documents(ctx, args)
        if tool_name == "get_document":
            return await self._get_document(ctx, args)
        if tool_name == "list_collections":
            return await self._list_collections(ctx)
        return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "Always available"

    async def _collection_ids(
        self, ctx: ExecutionContext, collection_name: str | None
    ) -> list[uuid.UUID]:
        collections = await self.collections.list_for_workspace(ctx.workspace_id)
        if collection_name:
            collections = [c for c in collections if c.name == collection_name]
        return [c.id for c in collections]

    async def _search_documents(self, ctx: ExecutionContext, args: dict[str, Any]) -> ToolResult:
        query = args.get("query")
        if not query or not isinstance(query, str):
            return ToolResult(ok=False, error="'query' is required")
        collection_ids = await self._collection_ids(ctx, args.get("collection"))
        if not collection_ids:
            return ToolResult(ok=True, content=[], meta={"result_count": 0})

        results = await hybrid_search(
            chunk_repo=self.chunks,
            document_repo=self.documents,
            gateway=self.gateway,
            settings=self.settings,
            workspace_id=ctx.workspace_id,
            collection_ids=collection_ids,
            query=query,
            run_id=ctx.run_id,
            top_k=int(args.get("top_k") or _DEFAULT_TOP_K),
        )
        return ToolResult(
            ok=True,
            content=[
                {
                    "document_id": str(r.chunk.document_id),
                    "title": r.document_title,
                    "page": r.chunk.page_start,
                    "citation": r.citation_label,
                    "text": r.chunk.content,
                    "score": r.score,
                }
                for r in results
            ],
            meta={"result_count": len(results)},
        )

    async def _get_document(self, ctx: ExecutionContext, args: dict[str, Any]) -> ToolResult:
        raw_id = args.get("document_id")
        try:
            document_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return ToolResult(ok=False, error=f"Invalid document_id {raw_id!r}")

        document = await self.documents.get(ctx.workspace_id, document_id)
        if document is None:
            return ToolResult(ok=False, error=f"Document {document_id} not found")

        ordered = await self.chunks.list_for_document(ctx.workspace_id, document_id)
        page_start, page_end = args.get("page_start"), args.get("page_end")
        if page_start is not None or page_end is not None:
            ordered = [
                c
                for c in ordered
                if (page_start is None or (c.page_start or 0) >= page_start)
                and (page_end is None or (c.page_end or c.page_start or 0) <= page_end)
            ]

        text = "\n\n".join(c.content for c in ordered)
        truncated = len(text) > _MAX_GET_DOCUMENT_CHARS
        return ToolResult(
            ok=True,
            content={"title": document.title, "text": text[:_MAX_GET_DOCUMENT_CHARS]},
            truncated=truncated,
            meta={"page_count": document.page_count},
        )

    async def _list_collections(self, ctx: ExecutionContext) -> ToolResult:
        collections = await self.collections.list_for_workspace(ctx.workspace_id)
        return ToolResult(
            ok=True,
            content=[{"name": c.name, "description": c.description} for c in collections],
        )
