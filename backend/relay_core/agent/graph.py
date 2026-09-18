"""Graph wiring (docs/system-design.md sections 8.2, 8.4).

`approval_gate` (Phase 5) sits between `execute_step` and `validate_step`, and is the only node
that can suspend the graph. `execute_step` routes to it by returning a `pending_approval_id`;
it interrupts, the worker exits, and the run is resumed later against the same checkpoint. On
the way back it returns to `execute_step`, not onward to `validate_step`: the decision has been
folded into the scratchpad as a function response, and the step's ReAct loop still has to run to
completion so the model can react to what did or didn't happen. A rejection is not a failure
route for the same reason — section 8.2's `approval_gate -> replan` edge is deliberately not
built (ADR-0013 decision 4), so a declined action comes back to the model as a rejection result
and the step summarizes around it. `validate_step` is the single entry to `replan`.

`replan` edges back to `check_capabilities` rather than straight to `next_step`, so a revised
plan that needs something this workspace doesn't have lands on the existing missing-capability
card instead of failing again one step later.

The checkpointer is passed in rather than constructed here, so the one real
`AsyncPostgresSaver` (built once per worker process by `get_postgres_checkpointer`,
below) and a test's `MemorySaver` go through the exact same `compile_graph` call —
`relay_core.agent.runner.run_agent_once` is what decides which one to use.
"""

import asyncio
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from relay_core.agent.deps import AgentDeps
from relay_core.agent.nodes.approval_gate import ApprovalGate
from relay_core.agent.nodes.ask_missing import AskMissing
from relay_core.agent.nodes.check_capabilities import CheckCapabilities
from relay_core.agent.nodes.direct_answer import DirectAnswer
from relay_core.agent.nodes.execute_step import ExecuteStep
from relay_core.agent.nodes.finalize import Finalize
from relay_core.agent.nodes.guard_input import GuardInput
from relay_core.agent.nodes.load_context import LoadContext
from relay_core.agent.nodes.next_step import NextStep
from relay_core.agent.nodes.plan import PlanNode
from relay_core.agent.nodes.replan import Replan
from relay_core.agent.nodes.route import Route
from relay_core.agent.nodes.synthesize import Synthesize
from relay_core.agent.nodes.validate_final import ValidateFinal
from relay_core.agent.nodes.validate_step import ValidateStep
from relay_core.agent.state import AgentState
from relay_core.config import Settings


def _route_after_execute(
    state: AgentState,
) -> Literal["approval_gate", "validate_step", "next_step"]:
    """A spent budget skips `validate_step`: retrying or replanning the step would only spend
    more. `next_step` skips the rest and hands over to `synthesize` (section 19.2)."""
    if state.pending_approval_id is not None:
        return "approval_gate"
    return "next_step" if state.budget_exhausted else "validate_step"


def _route_after_validate(state: AgentState) -> Literal["execute_step", "next_step", "replan"]:
    """`validate_step` resets a retried step back to `pending`; anything else (`done`/`failed`)
    means it's time to move on, unless it also set `replan_reason`, which is its verdict that
    the rest of the plan is worth rewriting. Reading all of this back off the merged state
    (rather than having `validate_step` return its own routing decision) keeps this a pure
    function of state, like every other conditional edge here."""
    assert state.plan is not None and state.current_step_id is not None
    if state.replan_reason is not None:
        return "replan"
    step = next(s for s in state.plan.steps if s.id == state.current_step_id)
    return "execute_step" if step.status == "pending" else "next_step"


def _route_after_validate_final(state: AgentState) -> Literal["synthesize", "finalize"]:
    """`validate_final` clears `final_answer` when it wants a revision, and leaves it in place
    otherwise. It bounds its own revisions, so this edge cannot loop."""
    return "synthesize" if state.final_answer is None else "finalize"


def build_graph(deps: AgentDeps) -> StateGraph[AgentState]:
    g = StateGraph(AgentState)
    g.add_node("load_context", LoadContext(deps))
    g.add_node("guard_input", GuardInput(deps))
    g.add_node("route", Route(deps))
    g.add_node("direct_answer", DirectAnswer(deps))
    g.add_node("plan", PlanNode(deps))
    g.add_node("check_capabilities", CheckCapabilities(deps))
    g.add_node("ask_missing", AskMissing(deps))
    g.add_node("next_step", NextStep(deps))
    g.add_node("execute_step", ExecuteStep(deps))
    g.add_node("approval_gate", ApprovalGate(deps))
    g.add_node("validate_step", ValidateStep(deps))
    g.add_node("replan", Replan(deps))
    g.add_node("synthesize", Synthesize(deps))
    g.add_node("validate_final", ValidateFinal(deps))
    g.add_node("finalize", Finalize(deps))

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "guard_input")
    g.add_conditional_edges(
        "guard_input", lambda s: "finalize" if s.route == "blocked" else "route"
    )
    g.add_conditional_edges("route", lambda s: "direct_answer" if s.route == "direct" else "plan")
    g.add_edge("plan", "check_capabilities")
    g.add_conditional_edges(
        "check_capabilities", lambda s: "ask_missing" if s.missing else "next_step"
    )
    g.add_edge("ask_missing", END)
    g.add_conditional_edges(
        "next_step", lambda s: "execute_step" if s.current_step_id else "synthesize"
    )
    g.add_conditional_edges("execute_step", _route_after_execute)
    g.add_edge("approval_gate", "execute_step")
    g.add_conditional_edges("validate_step", _route_after_validate)
    g.add_edge("replan", "check_capabilities")
    g.add_edge("synthesize", "validate_final")
    g.add_conditional_edges("validate_final", _route_after_validate_final)
    g.add_edge("direct_answer", "finalize")
    g.add_edge("finalize", END)
    return g


def compile_graph(deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    return build_graph(deps).compile(checkpointer=checkpointer)


_pool_lock = asyncio.Lock()
_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
_saver: AsyncPostgresSaver | None = None


async def get_postgres_checkpointer(settings: Settings) -> AsyncPostgresSaver:
    """One connection pool and one `AsyncPostgresSaver` per worker process,
    matching the production pattern in LangGraph's own docs for a long-running
    service (as opposed to `from_conn_string`'s context-manager form, which
    tears the pool down on exit — wrong for something meant to outlive a single
    call). `.setup()` is idempotent (`CREATE TABLE IF NOT EXISTS`), so calling
    it once behind this lock the first time a worker needs a checkpointer is
    enough; it does not need its own migration step.

    `row_factory=dict_row` is required, not a style choice: `AsyncPostgresSaver`
    reads query results by column name, and psycopg's default row factory
    returns plain tuples — without this every checkpoint read would fail.
    (`AsyncPostgresSaver.from_conn_string`, LangGraph's own single-connection
    convenience constructor, sets the same option.)
    """
    global _pool, _saver
    async with _pool_lock:
        if _saver is None:
            pool = AsyncConnectionPool(
                conninfo=settings.langgraph_db_url,
                connection_class=AsyncConnection[DictRow],
                max_size=20,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=False,
            )
            await pool.open()
            _pool = pool
            _saver = AsyncPostgresSaver(pool)
            await _saver.setup()
        return _saver
