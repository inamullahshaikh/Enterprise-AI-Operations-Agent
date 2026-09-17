# Phase 5 — Writes & approvals: status

**Last updated:** 17 September 2026
**Design doc:** [system-design.md](system-design.md) §13 (approvals), §10.3–10.4 (gmail/calendar), §28 Phase 5
**Working tree:** all of this is **uncommitted**. HEAD is `28bc522`.

Phase 5 is complete: all 19 tickets are done. This note started as a mid-phase handoff and now records what was built and why.

---

## Naming caveat (read first)

The repo internally uses the design doc's numbering, where **this is Phase 5**. The commit
labels are one behind: `28bc522 "Phase 3 completed"` actually contained design-doc Phase 3 *and*
Phase 4. The new migration is named `c7a41b9e5d20_phase5_approvals_and_policies` and code
comments say "Phase 5" throughout. Either keep that convention or renumber consistently — but
don't half-do it.

---



## Done — 19 of 19 tickets



### Workstream A — data model & policy foundation


| Ticket | Summary                                                                                                                                                                                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A1** | `approvals` + `workspace_policies` tables; `agent_runs.status` gains `awaiting_approval`/`expired`; `tool_calls.status` gains `pending_approval`/`rejected`/`skipped`; `idempotency_key` + its unique partial index; existing workspaces backfilled with a default policy |
| **A2** | Policy engine as a pure function (`needs_approval`, `blocked_recipients`), `WorkspacePolicyRepository`, and provisioning inside `WorkspaceRepository.create`                                                                                                              |




### Workstream B — approval mechanics in the graph


| Ticket | Summary                                                                                             |
| ------ | --------------------------------------------------------------------------------------------------- |
| **B1** | `relay_core/agent/scratchpad.py` — Gemini turns survive a checkpoint with thought signatures intact |
| **B2** | `ApprovalRepository`, `tool_calls` state transitions, `approval.required`/`approval.decided` events |
| **B3** | `execute_step` splits reads from writes and opens batch approvals                                   |
| **B4** | `approval_gate` node with `interrupt()`                                                             |
| **B5** | Graph wiring (`execute_step → approval_gate → execute_step`)                                        |




### Workstream C — resume path


| Ticket | Summary                                                                                                                                    |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **C1** | `GET /approvals` inbox + `POST /approvals/{id}/decision`, with per-row `required_role` RBAC, expiry check, partial batches and edited args |
| **C2** | `resume_agent_once` + the `resume_agent` Celery task + `get_resume_dispatcher`                                                             |
| **C3** | `relay_core/approvals.py` expiry sweep, on Celery beat every 5 minutes                                                                     |




### Workstream D — idempotency & durability


| Ticket | Summary                                                                                              |
| ------ | ---------------------------------------------------------------------------------------------------- |
| **D1** | Replay guard: the executor checks for an already-succeeded call and commits that row **out of band** |
| **D2** | Worker-kill durability test                                                                          |




### Workstream E — write connectors (mock-backed)


| Ticket | Summary                                                                                      |
| ------ | -------------------------------------------------------------------------------------------- |
| **E1** | `mocks/main.py` — Gmail + Calendar, seeded from the demo accounts, honours `Idempotency-Key` |
| **E2** | `gmail` connector — draft-first, four tools, recipient allow-list                            |
| **E3** | `google_calendar` connector — three tools                                                    |


**Tests:** 126 unit, 105 integration. `ruff` and `mypy` clean.

```bash
cd backend
python -m pytest tests/unit -q
pip install -e ../evals/relay_eval       # once; the harness test imports it
python -m pytest tests/integration -q   # needs a running Docker daemon
python -m ruff check relay_core relay_api relay_worker tests alembic
python -m mypy relay_core relay_api relay_worker
```

---



### Workstream F — evals

| Ticket | Summary |
| ------ | ------- |
| **F1** | `approval_compliance` suite (5 cases: approve, reject, partial batch, draft-then-send, calendar event). Any violation in **any** suite fails `relay-eval run --all --ci`, and this suite also needs a 100% pass rate so it can't pass vacuously. `test_eval_approval_compliance.py` disables the approval check on purpose and asserts the harness scores the violation. Breaking the scorer turns that test red. |
| **F2** | The harness parks on `awaiting_approval`, decides through the real `decide_approval` route function per the case's `approval_decision`, resumes, and loops. A new `full` profile installs postgres + mock gmail/google_calendar and resets the mock before each case. CI starts the mock service from source. |
| **F3** | `seed_demo` installs `gmail`/`google_calendar` against `MOCK_SERVICES_URL`. `task_success/renewals_001` is the section 21.3 renewal scenario: SQL, then drafts, with an approval in the middle. |

### Workstream G — docs

| Ticket | Summary |
| ------ | ------- |
| **G1** | [adr/0011](adr/0011-approvals-interrupt-resume-and-enforcement-in-code.md) covers interrupt/resume, the out-of-band commit, enforcement in code, and how violations are measured. The README has a Phase 5 status section and a note on the commit-label offset. |

`relay_core/connectors/installs.py` now does the installation bootstrap that `seed_demo` and the
harness had each copied for postgres.

---

## Not yet verified

The real-Gemini suites (`relay-eval run --suite approval_compliance`, `--suite task_success`)
have not been run. They need `GEMINI_API_KEY` plus the mock service. Run them locally or let CI
do it. The harness mechanics are covered by the scripted-model integration test above.

---

## Decisions made that aren't in the design doc

1. **The scratchpad is stored as** `model_dump()` **dicts, not** `types.Content`**.** Both round-trip
  thought signatures losslessly, but LangGraph warns that deserializing an unregistered type
   "will be blocked in a future version". See `relay_core/agent/scratchpad.py`.
2. **A model turn is answered all-or-nothing.** If any call in a turn needs approval, *nothing*
  in that turn runs — not even read calls beside it. Gemini requires a function response for
   every function call in a turn, so the turn can only be answered as a unit. `approval_gate`
   answers it by reading the proposed calls straight off the last model turn in the scratchpad,
   so nothing extra has to be checkpointed to describe them.
3. **The idempotency key derives from the** `tool_calls` **row id**, not the approval id. §13.3's
  `sha256(approval_id)` is only unique while an approval gates a single call; batch approvals
   gate many.
4. **The executor commits the tool-call row out of band.** This is the actual durability
  mechanism, not bookkeeping: the side effect has already happened outside Relay, and if the
   worker dies before its transaction commits, a rollback erases the only record that it did —
   and the retry sends again.
5. **A rejection returns a tool result to the model rather than failing the step.** The design's
  `approval_gate → replan` edge needs a `replan` node that doesn't exist until Phase 7, so a
   declined action comes back as a rejection result and the step summarizes around it.
6. `ExecutionContext` **gained** `policy` **and** `idempotency_key`**.** The recipient guard has to be
  enforced in the connector, because that's the only layer that knows which argument holds
   recipients; the key is attached per *call* by copying the context, which avoids changing the
   `Connector` ABC signature.
7. **One deliberate cross-tenant query.** `ApprovalRepository.list_expired_across_workspaces`
  is exempt from the tenant-scoping rule because a system sweep has no workspace to scope to.
   It's named loudly rather than hidden behind an optional `workspace_id=None`, and every row it
   returns is fed back through the ordinary tenant-scoped methods.

---



## Known rough edges

- Manifest `default:` values for `base_url` are not applied by the install endpoint, so
installing `gmail` requires passing `config.base_url` explicitly.
- `settings.use_mock_connectors` exists but is unused — the new connectors take `base_url` from
installation config, which `seed_demo` and the harness fill from `settings.mock_services_url`.
- An approved call whose edited arguments fail schema validation returns an error to the model
but leaves its `tool_calls` row at `pending_approval`.
- The harness's mock write count reads only the `primary` calendar.
- HubSpot stays out of scope, per [adr/0006](adr/0006-hubspot-dropped-from-scope.md).
- The approvals UI (ApprovalCard, inbox) is deliberately deferred to the frontend pass.

---



## One caution, learned the hard way

The worker-kill durability test initially **passed with its guard deliberately sabotaged**. The
cause was `monkeypatch.undo()` between the two resume attempts: it reverts *every* patch on the
fixture, including the one supplying the write tool, so the second resume had no tool to call
and the test went green for entirely the wrong reason.

F1 followed that lesson: the sabotage test disables the approval check, and the test itself was
checked by breaking the scorer to make sure it goes red. A compliance gate that can't fail is
worse than no gate, because it is believed.