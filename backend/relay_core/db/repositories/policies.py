import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.policies import WorkspacePolicy
from relay_core.policy import ApprovalRules, parse_approval_rules


class WorkspacePolicyRepository:
    """One row per workspace, keyed by `workspace_id` alone, so this doesn't extend
    `WorkspaceScopedRepository` (which assumes a surrogate `id` on a multi-row tenant table) —
    but `workspace_id` is still an explicit argument on every method, for the same reason
    (docs/system-design.md section 14.4).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, workspace_id: uuid.UUID) -> WorkspacePolicy:
        """Every workspace has a policy row: `WorkspaceRepository.create` writes one alongside
        the workspace, and the Phase 5 migration backfilled the ones that predate it. A missing
        row means the invariant broke, so this raises rather than inventing defaults — silently
        falling back would turn a provisioning bug into a governance hole."""
        policy = await self.session.get(WorkspacePolicy, workspace_id)
        if policy is None:
            raise ValueError(f"workspace_policies row missing for workspace {workspace_id}")
        return policy

    async def create_default(self, workspace_id: uuid.UUID) -> WorkspacePolicy:
        policy = WorkspacePolicy(workspace_id=workspace_id)
        self.session.add(policy)
        await self.session.flush()
        return policy

    async def approval_rules(self, workspace_id: uuid.UUID) -> ApprovalRules:
        return parse_approval_rules((await self.get(workspace_id)).approval_rules)
