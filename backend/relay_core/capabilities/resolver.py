"""Capability resolution (docs/system-design.md section 7.2), called from
`relay_core.agent.nodes.load_context`. An installed connector contributes the capabilities of its
enabled `tool_definitions` rows while the installation is active and healthy or degraded; its
manifest's `provides_capabilities` is catalog-only since Phase 6. Custom capabilities
(`custom.*`) come through the same way. "Is this capability available" only needs the union, not
a priority order: picking an installation is `relay_core.tools.registry.ToolRegistry`'s job.

`file_upload` is always available (section 10.8) and contributes `file.read` plus whatever
capabilities its attachments' column names suggest (section 7.2's fallback table, via the
heuristic in `relay_core.connectors.builtin.csv_profile.infer_capabilities`).
"""

import uuid

from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository


async def resolve_available_capabilities(
    tools_repo: ToolDefinitionRepository,
    attachments_repo: AttachmentRepository,
    documents_repo: DocumentRepository,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[str]:
    available: set[str] = {"file.read"}
    for row, _ in await tools_repo.list_bindable(workspace_id):
        available.update(row.capabilities)

    attachments = await attachments_repo.list_for_conversation(workspace_id, conversation_id)
    for attachment in attachments:
        available.update(attachment.inferred_capabilities)

    # `documents` is always-available like `file_upload` (no installation row, section 10.2/
    # 10.8) but only contributes `knowledge.search` once something has actually finished
    # ingesting — an empty knowledge base shouldn't make the planner think it can search one.
    if await documents_repo.has_any_ready(workspace_id):
        available.add("knowledge.search")

    return sorted(available)
