"""Capability resolution (docs/system-design.md section 7.2), called from
`relay_core.agent.nodes.load_context`. Trimmed per docs/adr/0009: no `capability_bindings`
table — an installation's manifest declares which capabilities it provides, and "is this
capability available" only needs a yes/no per connector type, not a priority-ordered list
(that ordering matters once something actually has to *pick* an installation, which is
`relay_core.tools.registry.ToolRegistry`'s job, not this one).

`file_upload` is always available (section 10.8) and contributes `file.read` plus whatever
capabilities its attachments' column names suggest (section 7.2's fallback table, via the
heuristic in `relay_core.connectors.builtin.csv_profile.infer_capabilities`).
"""

import uuid

from relay_core.connectors.manifest import load_manifests
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.documents import DocumentRepository

_HEALTHY_ENOUGH = ("healthy", "degraded")


async def resolve_available_capabilities(
    installations_repo: ConnectorInstallationRepository,
    attachments_repo: AttachmentRepository,
    documents_repo: DocumentRepository,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[str]:
    manifests = load_manifests()
    installations = await installations_repo.list_for_workspace(workspace_id)
    active_keys = {
        i.connector_key
        for i in installations
        if i.status == "active" and i.health in _HEALTHY_ENOUGH
    }

    available: set[str] = {"file.read"}
    for key in active_keys:
        manifest = manifests.get(key)
        if manifest is not None:
            available.update(manifest.provides_capabilities)

    attachments = await attachments_repo.list_for_conversation(workspace_id, conversation_id)
    for attachment in attachments:
        available.update(attachment.inferred_capabilities)

    # `documents` is always-available like `file_upload` (no installation row, section 10.2/
    # 10.8) but only contributes `knowledge.search` once something has actually finished
    # ingesting — an empty knowledge base shouldn't make the planner think it can search one.
    if await documents_repo.has_any_ready(workspace_id):
        available.add("knowledge.search")

    return sorted(available)
