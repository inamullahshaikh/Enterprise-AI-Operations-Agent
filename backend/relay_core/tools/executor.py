"""The tool executor (docs/system-design.md section 8.7): validates arguments against the
tool's own JSON Schema, runs the call (with retries for idempotent tools), records a
`tool_calls` row, and publishes `tool.started`/`tool.finished` events.

Section 8.7's `self.breakers.guard` arrives in Phase 7: an installation that has failed
`CircuitBreaker.FAILURE_THRESHOLD` times in a minute stops being called at all, and stops
binding. Only the `except` branch below counts as a failure — a `ToolResult(ok=False)` the
connector *returned* is a working connector answering a question, and argument validation never
reached the connector at all.

Every failure — a bad connector call, a timeout, a validation error — becomes a
`ToolResult(ok=False, ...)` handed back to the model as a function error, never an exception
that would crash the whole run (section 9.3: "Malformed function call ... returned to the
model as a function error so it can self-correct").
"""

import asyncio
import logging
import time
import uuid
from typing import Any

import jsonschema
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from relay_core.connectors.base import ExecutionContext, ToolResult
from relay_core.connectors.breaker import CircuitBreaker
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import TOOL_FINISHED, TOOL_STARTED
from relay_core.tools.registry import BoundTool

logger = logging.getLogger(__name__)

_RETRYABLE = (TimeoutError, ConnectionError, OSError)


class ToolExecutor:
    def __init__(
        self,
        tool_calls: ToolCallRepository,
        events: EventPublisher,
        breaker: CircuitBreaker | None = None,
        installations: ConnectorInstallationRepository | None = None,
    ) -> None:
        self.tool_calls = tool_calls
        self.events = events
        # Optional so the Phase 3 unit tests that build an executor with two repositories keep
        # working; a run always has both (`relay_core.agent.runner.build_agent_deps`).
        self.breaker = breaker
        self.installations = installations

    async def run(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        plan_step_id: str,
        bound: BoundTool,
        args: dict[str, Any],
        tool_call_id: uuid.UUID | None = None,
    ) -> ToolResult:
        """`tool_call_id` names an existing `pending_approval` row to execute instead of opening
        a new one — the path `approval_gate` takes for an approved write. It matters beyond
        tidiness: that row is the one carrying the `idempotency_key`, so reusing it is what ties
        a resumed execution to the call a human actually approved (section 13.3).
        """
        errors = _validate_args(bound, args)
        if errors:
            return ToolResult(ok=False, error=f"Invalid arguments: {'; '.join(errors)}")

        if await self._breaker_open(bound):
            # No `tool_calls` row and no events: nothing was attempted. The model gets an error
            # it can work around, and the installation gets the quiet it needs to recover.
            return ToolResult(
                ok=False,
                error=(
                    "Tool call skipped: this connector is failing repeatedly and has been taken "
                    "out of service for a couple of minutes. Try another approach or say so."
                ),
            )

        if tool_call_id is not None:
            replay = await self.tool_calls.succeeded_output(workspace_id, tool_call_id)
            if replay is not None:
                # This exact approved call already went through on an earlier attempt that died
                # before its checkpoint landed. Re-running it would send the email twice, so the
                # stored output is replayed instead (section 13.3).
                return ToolResult.model_validate(replay)
            record = await self.tool_calls.mark_running(workspace_id, tool_call_id)
            # Approvers can edit arguments before approving (FR-14), so what finally ran has to
            # replace what was proposed.
            record.arguments = args
        else:
            record = await self.tool_calls.start(
                workspace_id=workspace_id,
                run_id=run_id,
                plan_step_id=plan_step_id,
                installation_id=bound.installation_id,
                llm_name=bound.llm_name,
                arguments=args,
                risk=bound.spec.risk.value,
            )
        await self.events.publish(
            run_id,
            TOOL_STARTED,
            {
                "tool_call_id": str(record.id),
                "llm_name": bound.llm_name,
                "risk": bound.spec.risk.value,
            },
        )

        # Attach the key per call rather than per run, so a connector can forward it upstream.
        ctx = (
            bound.ctx.model_copy(update={"idempotency_key": record.idempotency_key})
            if record.idempotency_key
            else bound.ctx
        )
        started = time.monotonic()
        # One ceiling over the whole call, retries included: a remote MCP or OpenAPI server that
        # never answers must not hold the run open.
        timeout = asyncio.timeout(bound.spec.timeout_s)
        try:
            async with timeout:
                if bound.spec.idempotent:
                    result = await self._call_with_retry(bound, ctx, args)
                else:
                    result = await bound.connector.call_tool(ctx, bound.spec.name, args)
        except Exception as exc:  # noqa: BLE001 - any connector failure becomes a tool error
            reason = f"timed out after {bound.spec.timeout_s:g}s" if timeout.expired() else exc
            result = ToolResult(ok=False, error=f"Tool call failed: {reason}")
            await self._record_failure(bound, str(reason))
        else:
            await self._record_success(bound)
        latency_ms = int((time.monotonic() - started) * 1000)

        await self.tool_calls.finish(
            workspace_id,
            record.id,
            ok=result.ok,
            output=result.model_dump(mode="json") if result.ok else None,
            error=result.error,
            latency_ms=latency_ms,
        )
        if tool_call_id is not None and result.ok:
            # Commit the evidence immediately, out of step with the rest of the run's
            # transaction. This is the whole mechanism: the side effect has already happened
            # outside Relay, and if the worker dies before its transaction commits, a rollback
            # would erase the only record that it did — and the retry would send again. The
            # replay check above can only work against a row that outlived the crash.
            #
            # Safe to commit mid-run: `tool_calls` is an append-only audit trail, and LangGraph's
            # checkpoints already commit independently on their own connection pool.
            await self.tool_calls.session.commit()
        await self.events.publish(
            run_id,
            TOOL_FINISHED,
            {"tool_call_id": str(record.id), "llm_name": bound.llm_name, "ok": result.ok},
        )
        return result

    async def _breaker_open(self, bound: BoundTool) -> bool:
        if self.breaker is None or bound.installation_id is None:
            return False
        return await self.breaker.is_open(bound.installation_id)

    async def _record_failure(self, bound: BoundTool, reason: str) -> None:
        """Marks the installation degraded on the call that opened the breaker. A failed health
        write must not turn a tool error into a crashed run, so it is swallowed — the sweep
        (`relay_worker.tasks.connectors.recheck_unhealthy_installations`) corrects it later."""
        if self.breaker is None or bound.installation_id is None:
            return
        if not await self.breaker.record_failure(bound.installation_id):
            return
        if self.installations is None:
            return
        try:
            await self.installations.set_health(
                bound.ctx.workspace_id,
                bound.installation_id,
                health="degraded",
                message=f"Circuit breaker open after repeated failures: {reason}"[:500],
            )
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning(
                "could not mark installation %s degraded", bound.installation_id, exc_info=True
            )

    async def _record_success(self, bound: BoundTool) -> None:
        """A call that worked clears the failure count, and the health message the breaker set
        with it — `record_success` only reports True when there was state to clear, so a healthy
        connector's every call isn't a database write."""
        if self.breaker is None or bound.installation_id is None:
            return
        if not await self.breaker.record_success(bound.installation_id):
            return
        if self.installations is None:
            return
        try:
            await self.installations.set_health(
                bound.ctx.workspace_id,
                bound.installation_id,
                health="healthy",
                message="Recovered after a successful call",
            )
        except Exception:  # noqa: BLE001 - see `_record_failure`
            logger.warning(
                "could not clear health for installation %s", bound.installation_id, exc_info=True
            )

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        reraise=True,
    )
    async def _call_with_retry(
        self, bound: BoundTool, ctx: ExecutionContext, args: dict[str, Any]
    ) -> ToolResult:
        return await bound.connector.call_tool(ctx, bound.spec.name, args)


def _validate_args(bound: BoundTool, args: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft7Validator(bound.spec.input_schema)
    return [e.message for e in validator.iter_errors(args)]
