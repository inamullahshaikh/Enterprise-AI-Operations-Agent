"""Import every model module so `Base.metadata` is fully populated for Alembic
autogenerate and for `AsyncPostgresSaver`/test schema creation."""

from relay_core.db.models.approvals import Approval
from relay_core.db.models.attachments import Attachment
from relay_core.db.models.connectors import ConnectorCredential, ConnectorInstallation
from relay_core.db.models.conversations import Conversation, Message
from relay_core.db.models.documents import Collection, Document, DocumentChunk
from relay_core.db.models.identity import RefreshToken, User, Workspace, WorkspaceMember
from relay_core.db.models.llm import LLMCall, ModelPricing
from relay_core.db.models.memories import Memory
from relay_core.db.models.policies import WorkspacePolicy
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.models.tools import ToolDefinition

__all__ = [
    "AgentRun",
    "Approval",
    "Attachment",
    "Collection",
    "ConnectorCredential",
    "ConnectorInstallation",
    "Conversation",
    "Document",
    "DocumentChunk",
    "LLMCall",
    "Memory",
    "Message",
    "ModelPricing",
    "RefreshToken",
    "ToolCall",
    "ToolDefinition",
    "User",
    "Workspace",
    "WorkspaceMember",
    "WorkspacePolicy",
]
