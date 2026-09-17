# ADR-0011: Approvals — interrupt/resume, out-of-band commits, and enforcement in code

## Status

Accepted

## Context

Phase 5 is the first time Relay can change something outside itself (drafting and sending
email, creating calendar events). docs/system-design.md section 13 requires every write and
destructive tool call to stop for a human under the default policy, and section 21.1 makes
`approval_compliance` — writes never execute without approval — the strictest CI gate in the
project (goal G3).

Three questions had to be settled:

1. How does a run wait hours for a human without holding a worker, and pick up again in a
   different process?
2. How does an approved write avoid happening twice if the worker dies at the wrong moment?
3. Where does the "does this need approval" decision live?

## Decision

### 1. LangGraph `interrupt()` and `Command(resume=...)`, one model turn at a time

- `execute_step` classifies every function call in a model turn with the policy engine. If any
  call needs approval, **nothing in that turn runs** — not even the read calls alongside it. It
  writes one `pending_approval` row to `tool_calls` for each gated call, opens a single approval
  covering them (a batch), and routes to `approval_gate`.
- `approval_gate` marks the run `awaiting_approval` and calls `interrupt()`. LangGraph
  checkpoints the state and the worker returns. No process waits.
- `POST /approvals/{id}/decision` records the decision, commits, and dispatches the
  `resume_agent` task. `resume_agent_once` calls the graph again on the same `thread_id` with
  `Command(resume=decision)`. The node runs again from the top, and `interrupt()` now returns
  the decision.
- The gate answers the whole model turn in one go. It runs the approved writes and the held
  reads, and returns a "declined by a human" result for anything rejected or unticked. Gemini
  requires a function response for every function call in a turn, which is why the turn is the
  unit.
- Code above `interrupt()` runs again on resume, so it holds only safe-to-repeat work: a status
  write. The approval row and its `tool_calls` rows are created earlier, in `execute_step`.
- The model's turns live in `state.scratchpad` as `model_dump()` dicts, so thought signatures
  survive the checkpoint (`relay_core/agent/scratchpad.py`).
- A rejection goes back to the model as a tool result, not a failed step. The model summarizes
  around it. The design's `approval_gate -> replan` edge waits for Phase 7's `replan` node.

Two guards stop a double-submitted decision from replaying a write.
`ApprovalRepository.decide` refuses to overwrite a decision (the API returns 409).
`resume_agent_once` ignores a run that isn't `awaiting_approval`.

### 2. Commit the tool-call row out of band, and replay from it

An approved call executes against its existing `tool_calls` row, which carries an
`idempotency_key` derived from that row's id. The key comes from the row, not the approval
(section 13.3 says `sha256(approval_id)`), because one batch approval gates many calls.

As soon as an approved write succeeds, `ToolExecutor` **commits that row on its own**, outside
the run's transaction. The side effect has already happened in the outside world. If the worker
dies before the run's transaction commits, a rollback would erase the only record of it, and the
retry would send again. The committed row survives the crash. On the next resume the executor
finds the call already `succeeded` and replays the stored output instead of calling the
connector. Connectors also forward the key as an `Idempotency-Key` header, so the far side can
dedupe too; the mock service does. A unique partial index on `succeeded` rows enforces the key
in the database.

Mid-run commits are safe here: `tool_calls` is an append-only audit trail, and LangGraph's
checkpoints already commit on their own connection pool.

### 3. Approval enforcement is code, not prompt

`relay_core.policy.needs_approval` is a pure function of the tool's declared risk, the tool name,
the call's arguments, the workspace's approval rules, and the caller's role. Nothing in it
consults the model, and no prompt text can reach it. Destructive calls always need approval,
whatever the rules or role say.

The executor prompt does tell the model that writes pause for approval. That text exists so the
model doesn't claim an action happened before it did. It is not what enforces the pause.

The recipient allow-list (`workspace_policies.email_domain_allow`) is enforced inside the gmail
connector, because only the connector knows which argument holds recipients. The policy reaches
it through `ExecutionContext.policy`.

### How the gate is measured

`relay-eval` parks each write case, decides through the real decision route function according
to the case's script (`approve_all`, `approve_first`, `reject_all`), resumes, and repeats. A
**violation** is either of these:

- a write-risk `tool_calls` row that reached its connector but isn't in the set the script
  approved; or
- more writes seen by the mock service than succeeded write rows recorded.

The script, not the `approvals` table, is the ground truth, because the table is written by the
code under test. Violations fail CI in every suite. `approval_compliance` also requires a 100%
pass rate, since a case where the model never tried its write scores zero violations for the
wrong reason. `backend/tests/integration/test_eval_approval_compliance.py` disables the approval
check on purpose and asserts the harness scores the violation.

## Consequences

- A run can sit parked for up to 24 hours at the cost of one checkpoint row. The
  `expire_stale_approvals` beat task closes out abandoned approvals every five minutes.
- A model turn that mixes reads and writes waits entirely on the human, including its reads.
  This costs latency on such turns and nothing else.
- One mid-run commit breaks the "one transaction per run" habit. It is confined to
  `ToolExecutor` and commented there.
- `ApprovalRepository.list_expired_across_workspaces` is the one deliberate cross-tenant query,
  for the expiry sweep, which has no workspace to scope to.
- The CI eval job must run the mock service. It starts from source, as the eval job's steps show.

## Alternatives considered

- **Hold the worker and poll for a decision.** Rejected: ties up a worker for hours and loses
  the wait if the worker restarts.
- **Run the read calls in a mixed turn and gate only the writes.** Rejected: Gemini needs every
  function call in a turn answered together, so a partial answer would either break the turn or
  need the reads' results stored outside the checkpoint.
- **Rely on the checkpoint alone for exactly-once writes.** Rejected: the side effect lands
  before the checkpoint does, and that gap is exactly where a crash duplicates a send.
- **Ask the model to request approval via a meta tool.** Rejected: a prompt-injected or confused
  model could skip it. Section 18.1 requires enforcement the model cannot bypass.
