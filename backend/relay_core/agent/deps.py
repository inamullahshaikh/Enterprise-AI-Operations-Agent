"""`AgentDeps`: the dependency bundle every graph node takes in its constructor
(docs/system-design.md sections 8.4/8.5), so nodes stay plain, testable classes
instead of reaching for global state.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from relay_core.config import Settings
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.events.publisher import EventPublisher
from relay_core.llm.gateway import LLMGateway
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import ToolRegistry

# Hands a completed run to `relay_worker.tasks.memory` (docs/system-design.md section 12.2).
# A dependency rather than a direct `.delay()` in `finalize` for two reasons: the enqueue has to
# wait for the run's own transaction to commit, which only the runner knows how to arrange
# (`relay_core.agent.runner.after_commit_dispatcher`), and a test can swap it for a no-op the
# same way `relay_api.deps.get_ingest_dispatcher` is swapped.
MemoryDispatcher = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


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
    tool_definitions: ToolDefinitionRepository
    attachments: AttachmentRepository
    documents: DocumentRepository
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    approvals: ApprovalRepository
    policies: WorkspacePolicyRepository
    members: WorkspaceMemberRepository
    memories: MemoryRepository
    extract_memories: MemoryDispatcher
