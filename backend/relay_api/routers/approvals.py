"""Approval inbox & decision routes (docs/system-design.md sections 13.4, 15.4).

Unlike conversations and runs, approvals are **not** private to the user who triggered them: a
write action is a workspace-level event, and `approvals.required_role` exists precisely so a
policy can demand that somebody more senior than the requester signs off. So the inbox lists
every pending approval in the workspace, and the decision route checks the caller's role against
that row's `required_role` rather than against a fixed minimum.

Deciding is the only place a parked run can be restarted. Two independent guards stop a
double-submitted decision from replaying an approved write: `ApprovalRepository.decide` refuses
to overwrite an existing decision (surfaced here as 409), and `resume_agent_once` refuses to
resume a run that isn't `awaiting_approval`.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import (
    CurrentUser,
    ResumeDispatcher,
    get_resume_dispatcher,
    require_workspace_role,
)
from relay_core.db.models.approvals import Approval
from relay_core.db.repositories.approvals import AlreadyDecidedError, ApprovalRepository
from relay_core.db.session import get_session
from relay_core.security.rbac import Role, has_at_least

router = APIRouter(prefix="/workspaces/{workspace_id}/approvals", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    summary: str
    status: str
    required_role: str
    tool_call_ids: list[uuid.UUID]
    proposed_args: list[dict[str, Any]]
    requested_by: uuid.UUID
    expires_at: str
    created_at: str

    @classmethod
    def from_model(cls, a: Approval) -> "ApprovalOut":
        return cls(
            id=a.id,
            run_id=a.run_id,
            summary=a.summary,
            status=a.status,
            required_role=a.required_role,
            tool_call_ids=list(a.tool_call_ids),
            proposed_args=list(a.proposed_args),
            requested_by=a.requested_by,
            expires_at=a.expires_at.isoformat(),
            created_at=a.created_at.isoformat(),
        )


class DecisionRequest(BaseModel):
    action: Literal["approve", "reject"]
    # Keyed by `tool_calls` row id so a batch can be corrected item by item (FR-14).
    edited_args: dict[uuid.UUID, dict[str, Any]] = Field(default_factory=dict)
    # Which items of a batch to approve. Omitted means all of them (section 13.4).
    item_ids: list[uuid.UUID] | None = None
    reason: str | None = None


class DecisionResponse(BaseModel):
    approval_id: uuid.UUID
    status: str
    run_id: uuid.UUID


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    workspace_id: uuid.UUID = Path(...),
    status_filter: Literal["pending"] = Query("pending", alias="status"),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalOut]:
    """Only `pending` is listable for now — a decided approval is history, and the run inspector
    (Phase 8) is what will surface it alongside the rest of the run's audit trail."""
    approvals = await ApprovalRepository(session).list_pending(workspace_id)
    return [ApprovalOut.from_model(a) for a in approvals]


@router.post("/{approval_id}/decision", response_model=DecisionResponse)
async def decide_approval(
    body: DecisionRequest,
    workspace_id: uuid.UUID = Path(...),
    approval_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
    dispatch: ResumeDispatcher = Depends(get_resume_dispatcher),
) -> DecisionResponse:
    approvals = ApprovalRepository(session)
    approval = await approvals.get(workspace_id, approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    if not has_at_least(current.membership.role, approval.required_role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This action must be approved by a {approval.required_role} or above",
        )
    if approval.expires_at <= datetime.now(UTC):
        # The watchdog will also sweep this up, but a decision arriving after the window has
        # closed must not resume the run — the checkpoint may already have been expired out.
        raise HTTPException(status.HTTP_409_CONFLICT, "This approval has expired")

    approved_ids = _approved_ids(approval, body)
    decided_status = _decided_status(approval, body, approved_ids)
    try:
        await approvals.decide(
            workspace_id,
            approval_id,
            status=decided_status,
            decided_by=current.user.id,
            final_args=_final_args(approval, body),
            reason=body.reason,
        )
    except AlreadyDecidedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    decision: dict[str, Any] = {
        "action": body.action,
        "item_ids": [str(i) for i in approved_ids],
        "edited_args": {str(k): v for k, v in body.edited_args.items()},
        "reason": body.reason,
    }
    # Commit before dispatching: the worker resumes in a separate process against a separate
    # connection and reads this decision back, so `get_session`'s post-handler commit would be
    # too late (same reasoning as `conversations.send_message`).
    await session.commit()
    await dispatch(workspace_id, approval.run_id, decision)

    return DecisionResponse(
        approval_id=approval.id, status=decided_status, run_id=approval.run_id
    )


def _approved_ids(approval: Approval, body: DecisionRequest) -> list[uuid.UUID]:
    if body.action != "approve":
        return []
    if body.item_ids is None:
        return list(approval.tool_call_ids)
    wanted = {str(i) for i in body.item_ids}
    return [row_id for row_id in approval.tool_call_ids if str(row_id) in wanted]


def _decided_status(
    approval: Approval, body: DecisionRequest, approved_ids: list[uuid.UUID]
) -> str:
    """Approving nothing is a rejection, however it was phrased — otherwise an empty `item_ids`
    would record an `approved` row that authorized no actual call."""
    if body.action == "reject" or not approved_ids:
        return "rejected"
    if len(approved_ids) < len(approval.tool_call_ids):
        return "partially_approved"
    return "approved"


def _final_args(approval: Approval, body: DecisionRequest) -> list[dict[str, Any]] | None:
    """What will actually be executed, recorded alongside the model's original `proposed_args`
    so the audit trail keeps both."""
    if not body.edited_args:
        return None
    edits = {str(k): v for k, v in body.edited_args.items()}
    return [
        {**proposed, "args": edits.get(str(row_id), proposed.get("args", {}))}
        for row_id, proposed in zip(approval.tool_call_ids, approval.proposed_args, strict=False)
    ]
