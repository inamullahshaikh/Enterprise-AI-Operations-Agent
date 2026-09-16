"""`AgentDeps`: the dependency bundle every graph node takes in its constructor
(docs/system-design.md sections 8.4/8.5), so nodes stay plain, testable classes
instead of reaching for global state.
"""

from dataclasses import dataclass

from relay_core.config import Settings
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.llm.gateway import LLMGateway
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import ToolRegistry


@dataclass
class AgentDeps:
    gateway: LLMGateway
    events: EventPublisher
    settings: Settings
    conversations: ConversationRepository
    messages: MessageRepository
    runs: AgentRunRepository
    llm_calls: LLMCallRepository
    tool_calls: ToolCallRepository
    connector_installations: ConnectorInstallationRepository
    attachments: AttachmentRepository
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
