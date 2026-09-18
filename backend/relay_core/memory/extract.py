"""Memory extraction after a completed run (docs/system-design.md section 12.2).

The model only proposes; everything that matters is enforced here, in code — the same split as
`relay_core.capabilities.tagger`. A prompt that says "never store secrets" is a request; the
confidence floor, the secret-shape filter, the scope downgrade and the duplicate check below are
the guarantee.

Two rules read oddly until you know what the extractor is shown. Section 12.2's "never store
credentials" also covers "an argument that came from `secrets`" — that can't reach here, because
the inputs are the user message, the final answer and the step summaries, never `tool_calls.
input`. The regex below is for the other direction: a user who pastes a key into chat.

Deduplication is by embedding, not by text: "prefers formal drafts" and "wants emails written
formally" are the same memory, and storing both would mean retrieval spends two of its five
slots saying one thing. `supersedes_id` is the model's own version of that judgement — it sees
the existing memories, so it can say "this replaces that" for a preference that *changed* rather
than one that was merely restated.
"""

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.models.memories import Memory
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import LIGHT
from relay_core.llm.schemas import parse_structured
from relay_core.security.rbac import has_at_least

MIN_CONFIDENCE = 0.75
# Section 12.2's "> 0.92 -> update existing", expressed as the cosine *distance* `nearest` takes.
DUPLICATE_DISTANCE = 1.0 - 0.92
_MAX_PER_RUN = 5
_MAX_EXISTING_SHOWN = 30

# Strings shaped like a credential value. `relay_core.security.scrub` redacts these from logs
# and audit rows, so there is one definition of "looks like a key".
SECRET_VALUE_SHAPES = (
    r"\bsk-[A-Za-z0-9_-]{12,}"
    r"|AIza[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"|[A-Za-z0-9+/]{40,}={0,2}"
)
# Content shaped like a credential, or talking about one, whatever the model thought it was.
# Deliberately broad: a dropped memory costs nothing, a stored key is a breach.
_SECRET_SHAPES = re.compile(
    r"(?i)(?:"
    r"\b(?:(?:api|secret|private|access|auth|license)[ _-]?keys?|secrets?|passwords?|passwd"
    r"|passphrases?|credentials?|(?:access|refresh|auth|bearer)[ _-]?tokens?|bearer)\b"
    rf"|{SECRET_VALUE_SHAPES}"
    r")"
)

_SYSTEM_PROMPT = """\
You extract durable memories from one finished conversation turn with Relay, an operations
agent inside a company workspace.

Extract only what is worth remembering NEXT time this person talks to Relay:
- preference: how they want work done ("wants email drafts in a formal tone").
- fact: a stable truth about them, their team or their company ("owns the EMEA accounts").
- procedure: how a recurring task should be carried out here ("renewal checks run on the 1st").

Do NOT extract:
- anything specific to this one request (what was asked, what the answer was, today's numbers).
- anything a fresh lookup would answer better — memory is for what tools cannot fetch.
- secrets, credentials, API keys, passwords, or sensitive personal data. Never. If the text
  contains one, leave it out entirely rather than describing it.
- anything you are guessing at. confidence below 0.75 is discarded, so say what you mean.

scope: "user" for something true of this person, "workspace" for something true of the whole
company. Prefer "user" when unsure.
confidence: 0 to 1, how sure you are this is durable and correct.
supersedes_id: if a memory below states something this replaces (they changed their mind, the
fact moved on), give that memory's id. If it merely repeats one, do not extract it at all.

Memories already stored for this person:
{existing}

Return JSON matching the schema. An empty list is the right answer for most turns.
"""


class ExtractedMemory(BaseModel):
    content: str
    kind: Literal["preference", "fact", "procedure"]
    scope: Literal["user", "workspace"]
    confidence: float
    supersedes_id: str | None = None


class _Extraction(BaseModel):
    memories: list[ExtractedMemory] = Field(default_factory=list)


def looks_like_secret(content: str) -> bool:
    return _SECRET_SHAPES.search(content) is not None


async def extract_memories(
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession,
    gateway: LLMGateway,
    settings: Settings,
) -> list[Memory]:
    """Extracts, filters and stores the memories one completed run produced. Returns the rows
    written or updated, which is what the tests assert on; the caller commits.

    Returns `[]` without calling the model for anything not worth extracting from: memory turned
    off for the workspace, a run that failed, or one the input guard blocked.
    """
    policy = await WorkspacePolicyRepository(session).get(workspace_id)
    if not policy.memory_enabled:
        return []

    runs = AgentRunRepository(session)
    run = await runs.get(workspace_id, run_id)
    if run is None or run.status != "completed" or run.route == "blocked":
        return []

    transcript = await _transcript(session, workspace_id, run)
    if not transcript:
        return []

    memories = MemoryRepository(session)
    existing = await memories.list_visible_to(workspace_id, run.user_id)
    existing = existing[:_MAX_EXISTING_SHOWN]
    existing_by_id = {str(m.id): m for m in existing}

    try:
        resp = await gateway.generate(
            role=LIGHT,
            system=_SYSTEM_PROMPT.format(existing=_render_existing(existing)),
            contents=transcript,
            workspace_id=workspace_id,
            run_id=run_id,
            response_schema=_Extraction,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - an unavailable extractor means no memories, never a
        # failed run: this task is downstream of an answer the user already has.
        return []

    parsed = parse_structured(resp, _Extraction)
    if parsed is None:
        return []

    # A workspace-scope memory is visible to everyone in the workspace, so proposing one is an
    # admin action. Anyone else's is kept as their own — stored, not discarded, because the
    # observation is usually still true of the person who made it.
    member = await WorkspaceMemberRepository(session).get(workspace_id, run.user_id)
    may_write_workspace_scope = member is not None and has_at_least(member.role, "admin")

    candidates = [
        m
        for m in parsed.memories
        if m.confidence >= MIN_CONFIDENCE
        and m.content.strip()
        and not looks_like_secret(m.content)
    ][:_MAX_PER_RUN]
    if not candidates:
        return []

    vectors = await gateway.embed(
        [m.content for m in candidates], task="RETRIEVAL_DOCUMENT", settings=settings
    )

    written: list[Memory] = []
    for candidate, vector in zip(candidates, vectors, strict=True):
        scope = candidate.scope if may_write_workspace_scope else "user"
        named = existing_by_id.get(candidate.supersedes_id or "")
        superseded = _writable(named, may_write_workspace_scope)
        if superseded is None:
            near = await memories.nearest(
                workspace_id,
                run.user_id,
                vector,
                limit=1,
                max_distance=DUPLICATE_DISTANCE,
            )
            superseded = _writable(near[0] if near else None, may_write_workspace_scope)

        if superseded is not None:
            written.append(
                await memories.update(
                    superseded,
                    content=candidate.content,
                    kind=candidate.kind,
                    confidence=candidate.confidence,
                    embedding=vector,
                    embedding_model=settings.embedding_model,
                    source_run_id=run_id,
                    is_active=True,
                )
            )
            continue

        written.append(
            await memories.create(
                workspace_id=workspace_id,
                user_id=run.user_id,
                scope=scope,
                kind=candidate.kind,
                content=candidate.content,
                confidence=candidate.confidence,
                embedding=vector,
                embedding_model=settings.embedding_model,
                source_run_id=run_id,
            )
        )
    return written


def _writable(memory: Memory | None, may_write_workspace_scope: bool) -> Memory | None:
    """A workspace-scope row is everybody's, so a member's run may not overwrite one by claiming
    it supersedes theirs — or by merely saying something close enough to it. Their observation
    is stored as a new user-scope memory instead."""
    if memory is not None and memory.scope == "workspace" and not may_write_workspace_scope:
        return None
    return memory


def _render_existing(existing: list[Memory]) -> str:
    if not existing:
        return "(none yet)"
    return "\n".join(f"{m.id} [{m.scope}/{m.kind}] {m.content}" for m in existing)


async def _transcript(session: AsyncSession, workspace_id: uuid.UUID, run: Any) -> str:
    """What the run said and did, as the extractor sees it: the user's message, the answer, and
    each step's summary. Step results come from `agent_runs.plan` rather than state, because
    this runs after the graph has finished and its checkpoint is no longer being read."""
    messages = MessageRepository(session)
    parts: list[str] = []

    trigger = (
        await messages.get(workspace_id, run.trigger_message_id) if run.trigger_message_id else None
    )
    if trigger is not None:
        parts.append(f"User said:\n{trigger.content}")

    final = (
        await messages.get(workspace_id, run.final_message_id) if run.final_message_id else None
    )
    if final is not None:
        parts.append(f"Relay answered:\n{final.content}")

    steps = (run.plan or {}).get("steps") or []
    summaries = [
        f"- {s.get('goal')}: {s.get('result_summary')}" for s in steps if s.get("result_summary")
    ]
    if summaries:
        parts.append("Steps run:\n" + "\n".join(summaries))

    return "\n\n".join(parts)
