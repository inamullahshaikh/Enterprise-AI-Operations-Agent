"""Event name constants (docs/system-design.md section 16.3), trimmed to the subset built so
far. Approval/artifact/usage events are Phase 5+, once there's something that produces them.
"""

RUN_STARTED = "run.started"
PLAN_CREATED = "plan.created"
CAPABILITIES_MISSING = "capabilities.missing"
QUESTION_ASKED = "question.asked"
TOKEN = "token"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
STEP_STARTED = "step.started"
STEP_FINISHED = "step.finished"
TOOL_STARTED = "tool.started"
TOOL_FINISHED = "tool.finished"

# Events that end an SSE stream (docs/system-design.md section 16.2). Phase 2 has
# no `awaiting_approval`/`cancelled` run outcomes yet, so `run.completed` alone
# also covers `ask_missing`'s awaiting_input outcome — the run's `status` field
# (fetched via GET /runs/{id}, or in this event's own payload) is what tells a
# client completed from awaiting_input, not the event type.
TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED})
