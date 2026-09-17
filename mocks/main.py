"""Deterministic stand-ins for Gmail and Google Calendar (docs/system-design.md sections 10.3,
10.4, 21.2).

Evals have to be reproducible and free, and a portfolio demo can't depend on a real inbox, so
every external call these two connectors make lands here instead. Phase 7 swaps in real Google
OAuth against a test-mode app; until then this *is* the backend, and the connectors are written
against it exactly as they will be written against the real APIs.

State is in-memory and process-local — it resets when the container restarts, and `POST /_reset`
resets it on demand so a test can start from a known inbox. Seeded from the same Northstar
Analytics accounts as `demo/seed/02_data.sql`, so a run that reads the demo database and a run
that reads this inbox talk about the same companies and the same people.

**Idempotency-Key is honoured on every write.** That isn't decoration: Relay forwards
`sha256(tool_call_id)` on approved writes (section 13.3), and the guarantee that a retried send
doesn't produce a second email is only end-to-end if the far side dedupes too. Replaying a key
returns the original response rather than creating a second record.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="Relay Mock Services")


class _Store:
    """All mutable state in one object so `/_reset` is a single rebind."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = _seed_messages()
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = _seed_events()
        # Idempotency-Key -> the response first returned for it.
        self.replays: dict[str, dict[str, Any]] = {}


def _seed_messages() -> list[dict[str, Any]]:
    base = datetime.now(UTC) - timedelta(days=6)
    people = [
        ("jordan@acmerobotics.example", "Acme Robotics", "Question about our renewal terms"),
        ("priya@globex.example", "Globex", "Seat count for next year"),
        ("sam@northwindlabs.example", "Northwind Labs", "Usage report looks off"),
        ("morgan@initech.example", "Initech", "Downgrading to starter"),
    ]
    return [
        {
            "id": f"msg-{i + 1}",
            "thread_id": f"thread-{i + 1}",
            "from": email,
            "to": "ops@northstar.example",
            "subject": subject,
            "snippet": f"Hi team — {subject.lower()}...",
            "body": (
                f"Hi team,\n\n{subject}. Could someone from {company} account management "
                f"get back to me this week?\n\nThanks."
            ),
            "date": (base + timedelta(days=i)).isoformat(),
        }
        for i, (email, company, subject) in enumerate(people)
    ]


def _seed_events() -> list[dict[str, Any]]:
    start = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    return [
        {
            "id": f"evt-{i + 1}",
            "calendar_id": "primary",
            "title": title,
            "start": (start + timedelta(days=i, hours=offset)).isoformat(),
            "end": (start + timedelta(days=i, hours=offset + 1)).isoformat(),
            "attendees": attendees,
            "description": "",
        }
        for i, (title, offset, attendees) in enumerate(
            [
                ("Renewal sync — Acme Robotics", 1, ["jordan@acmerobotics.example"]),
                ("Pipeline review", 3, ["ops@northstar.example"]),
                ("QBR — Globex", 2, ["priya@globex.example"]),
            ]
        )
    ]


store = _Store()


def _replayed(key: str | None) -> dict[str, Any] | None:
    return store.replays.get(key) if key else None


def _remember(key: str | None, response: dict[str, Any]) -> dict[str, Any]:
    if key:
        store.replays[key] = response
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_reset")
async def reset() -> dict[str, str]:
    """Test-only: restores the seeded inbox and calendar and forgets every draft, sent message
    and idempotency key."""
    global store
    store = _Store()
    return {"status": "reset"}


# ---------------------------------------------------------------------------- Gmail


@app.get("/gmail/messages")
async def search_messages(
    q: str = Query("", description="Case-insensitive substring match"),
    max_results: int = Query(10, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Substring matching, not Gmail's real query syntax — enough for the agent to find a
    thread by sender or subject, and deterministic, which the real operators are not."""
    needle = q.strip().lower()
    matches = [
        m
        for m in store.messages
        if not needle
        or needle in m["subject"].lower()
        or needle in m["from"].lower()
        or needle in m["body"].lower()
    ]
    return [{k: v for k, v in m.items() if k != "body"} for m in matches[:max_results]]


@app.get("/gmail/messages/{message_id}")
async def get_message(message_id: str) -> dict[str, Any]:
    for message in store.messages:
        if message["id"] == message_id:
            return message
    raise HTTPException(404, "Message not found")


class DraftRequest(BaseModel):
    to: list[str]
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)
    thread_id: str | None = None


@app.post("/gmail/drafts", status_code=201)
async def create_draft(
    body: DraftRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")
) -> dict[str, Any]:
    replay = _replayed(idempotency_key)
    if replay is not None:
        return replay
    draft_id = f"draft-{uuid.uuid4().hex[:8]}"
    draft = {
        "draft_id": draft_id,
        "to": body.to,
        "cc": body.cc,
        "subject": body.subject,
        "body": body.body,
        "thread_id": body.thread_id,
        "created_at": datetime.now(UTC).isoformat(),
        "sent": False,
    }
    store.drafts[draft_id] = draft
    return _remember(idempotency_key, draft)


@app.get("/gmail/drafts")
async def list_drafts() -> list[dict[str, Any]]:
    """Not a tool the agent calls — a test/demo affordance for asserting what was created."""
    return list(store.drafts.values())


@app.post("/gmail/drafts/{draft_id}/send")
async def send_draft(
    draft_id: str, idempotency_key: str | None = Header(None, alias="Idempotency-Key")
) -> dict[str, Any]:
    replay = _replayed(idempotency_key)
    if replay is not None:
        return replay
    draft = store.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    if draft["sent"]:
        raise HTTPException(409, "Draft has already been sent")
    draft["sent"] = True
    sent = {
        "message_id": f"sent-{uuid.uuid4().hex[:8]}",
        "draft_id": draft_id,
        "to": draft["to"],
        "subject": draft["subject"],
        "sent_at": datetime.now(UTC).isoformat(),
    }
    store.sent.append(sent)
    return _remember(idempotency_key, sent)


@app.get("/gmail/sent")
async def list_sent() -> list[dict[str, Any]]:
    """Test/demo affordance: the assertion surface for "exactly one email went out"."""
    return store.sent


# ------------------------------------------------------------------------- Calendar


@app.get("/calendar/events")
async def list_events(
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str = "primary",
) -> list[dict[str, Any]]:
    events = [e for e in store.events if e["calendar_id"] == calendar_id]
    if time_min:
        events = [e for e in events if e["end"] >= time_min]
    if time_max:
        events = [e for e in events if e["start"] <= time_max]
    return sorted(events, key=lambda e: e["start"])


class FreeSlotsRequest(BaseModel):
    attendees: list[str] = Field(default_factory=list)
    duration_min: int = 30
    time_min: str | None = None
    time_max: str | None = None


@app.post("/calendar/free-slots")
async def find_free_slots(body: FreeSlotsRequest) -> list[dict[str, Any]]:
    """Walks working hours (09:00-17:00 UTC) over the next five days and returns the gaps that
    none of `attendees` is already booked into. Deliberately simple — the point is a stable,
    checkable answer, not a scheduling engine."""
    busy = [
        (e["start"], e["end"])
        for e in store.events
        if not body.attendees or set(body.attendees) & set(e["attendees"])
    ]
    duration = timedelta(minutes=body.duration_min)
    day = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    slots: list[dict[str, Any]] = []
    for offset in range(5):
        cursor = day + timedelta(days=offset)
        end_of_day = cursor.replace(hour=17)
        while cursor + duration <= end_of_day:
            slot_end = cursor + duration
            overlaps = any(
                start < slot_end.isoformat() and cursor.isoformat() < end for start, end in busy
            )
            if not overlaps:
                slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
            cursor = slot_end
    return slots[:10]


class EventRequest(BaseModel):
    title: str
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list)
    description: str = ""
    calendar_id: str = "primary"


@app.post("/calendar/events", status_code=201)
async def create_event(
    body: EventRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")
) -> dict[str, Any]:
    replay = _replayed(idempotency_key)
    if replay is not None:
        return replay
    event = {
        "id": f"evt-{uuid.uuid4().hex[:8]}",
        "calendar_id": body.calendar_id,
        "title": body.title,
        "start": body.start,
        "end": body.end,
        "attendees": body.attendees,
        "description": body.description,
    }
    store.events.append(event)
    return _remember(idempotency_key, event)
