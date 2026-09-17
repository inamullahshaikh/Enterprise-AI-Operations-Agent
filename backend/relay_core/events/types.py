"""Event name constants (docs/system-design.md section 16.3), trimmed to the subset built so
far. Artifact and usage events are Phase 6+, once there's something that produces them.
"""

RUN_STARTED = "run.started"
PLAN_CREATED = "plan.created"
PLAN_UPDATED = "plan.updated"
CAPABILITIES_MISSING = "capabilities.missing"
QUESTION_ASKED = "question.asked"
TOKEN = "token"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
STEP_STARTED = "step.started"
STEP_FINISHED = "step.finished"
TOOL_STARTED = "tool.started"
TOOL_FINISHED = "tool.finished"
APPROVAL_REQUIRED = "approval.required"
APPROVAL_DECIDED = "approval.decided"

# Events that end an SSE stream (docs/system-design.md section 16.2).
#
# `approval.required` is terminal because the worker *exits* at that point: the graph has hit
# `interrupt()`, the run is parked on a checkpoint as `awaiting_approval`, and nothing further
# will be published on this stream until somebody decides. Holding the connection open would
# mean holding it for up to `Approval.DEFAULT_EXPIRY_HOURS`. The resumed run publishes to the
# same `run:{run_id}` stream, so a client that reconnects with `Last-Event-ID` after submitting
# a decision picks up exactly where it left off.
#
# `run.completed` also covers `ask_missing`'s `awaiting_input` outcome — the run's `status`
# field (in this event's payload, or via GET /runs/{id}) is what distinguishes completed from
# awaiting_input, not the event type.
TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, APPROVAL_REQUIRED})
