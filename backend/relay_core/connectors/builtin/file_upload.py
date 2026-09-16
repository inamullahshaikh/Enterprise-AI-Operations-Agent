"""The `file_upload` connector (docs/system-design.md section 10.8): always available in
every workspace, no installation/config/secrets — the main fallback when a data connector
isn't connected. `read_text` (for PDF/DOCX-derived attachments) isn't implemented yet;
Phase 3 attachments are CSV only (docs/adr/0009), so there's no text content to read.
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
from relay_core.connectors.builtin.csv_profile import read_rows
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.storage.object_store import ObjectStore


class FileUploadConnector(Connector):
    key = "file_upload"
    display_name = "File uploads"
    auth_type = AuthType.NONE

    def __init__(self, attachments: AttachmentRepository, object_store: ObjectStore) -> None:
        self.attachments = attachments
        self.object_store = object_store

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        # Capabilities are computed per call, not a fixed class-level list: which of
        # subscription.read/usage.read/customer.read this connector can actually serve
        # depends on what's been uploaded to *this* conversation (relay_core.capabilities.resolver
        # does the same inference for `check_capabilities` to see before any tool runs).
        attachments = await self.attachments.list_for_conversation(
            ctx.workspace_id, ctx.conversation_id
        )
        inferred = sorted({cap for a in attachments for cap in a.inferred_capabilities})
        capabilities = ["file.read", *inferred]
        return [
            ToolSpec(
                name="list_attachments",
                description="List files attached to this conversation.",
                input_schema={"type": "object", "properties": {}},
                risk=Risk.READ,
                capabilities=capabilities,
            ),
            ToolSpec(
                name="read_table",
                description=(
                    "Read rows from an attached CSV file by its attachment id "
                    "(from list_attachments)."
                ),
                input_schema={
                    "type": "object",
                    "required": ["attachment_id"],
                    "properties": {
                        "attachment_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 200},
                    },
                },
                risk=Risk.READ,
                capabilities=capabilities,
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        if tool_name == "list_attachments":
            return await self._list_attachments(ctx)
        if tool_name == "read_table":
            return await self._read_table(ctx, args)
        return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "Always available"

    async def _list_attachments(self, ctx: ExecutionContext) -> ToolResult:
        attachments = await self.attachments.list_for_conversation(
            ctx.workspace_id, ctx.conversation_id
        )
        return ToolResult(
            ok=True,
            content=[
                {
                    "attachment_id": str(a.id),
                    "filename": a.filename,
                    "row_count": (a.profile or {}).get("row_count"),
                    "columns": (a.profile or {}).get("columns"),
                }
                for a in attachments
            ],
        )

    async def _read_table(self, ctx: ExecutionContext, args: dict[str, Any]) -> ToolResult:
        raw_id = args.get("attachment_id")
        try:
            attachment_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return ToolResult(ok=False, error=f"Invalid attachment_id {raw_id!r}")

        attachment = await self.attachments.get(ctx.workspace_id, attachment_id)
        if attachment is None or attachment.conversation_id != ctx.conversation_id:
            return ToolResult(
                ok=False, error=f"Attachment {attachment_id} not found in this conversation"
            )

        raw = await self.object_store.get_bytes(attachment.blob_key)
        rows, truncated = read_rows(raw, limit=int(args.get("limit") or 200))
        return ToolResult(ok=True, content=rows, truncated=truncated, meta={"row_count": len(rows)})
