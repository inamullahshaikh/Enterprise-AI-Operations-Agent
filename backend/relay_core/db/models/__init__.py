"""Import every model module so `Base.metadata` is fully populated for Alembic
autogenerate and for `AsyncPostgresSaver`/test schema creation."""

from relay_core.db.models.attachments import Attachment
from relay_core.db.models.connectors import ConnectorCredential, ConnectorInstallation
from relay_core.db.models.conversations import Conversation, Message
from relay_core.db.models.identity import RefreshToken, User, Workspace, WorkspaceMember
from relay_core.db.models.llm import LLMCall, ModelPricing
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall

__all__ = [
    "AgentRun",
    "Attachment",
    "ConnectorCredential",
    "ConnectorInstallation",
    "Conversation",
    "LLMCall",
    "Message",
    "ModelPricing",
    "RefreshToken",
    "ToolCall",
    "User",
    "Workspace",
    "WorkspaceMember",
]
