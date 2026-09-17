"""The tool executor (docs/system-design.md section 8.7): validates arguments against the
tool's own JSON Schema, runs the call (with retries for idempotent tools), records a
`tool_calls` row, and publishes `tool.started`/`tool.finished` events. No circuit breaker yet
(section 8.7's `self.breakers.guard`) — that matters once a capability has more than one
competing installation to fail over between, which Phase 3 never does (docs/adr/0009).

Every failure — a bad connector call, a timeout, a validation error — becomes a
`ToolResult(ok=False, ...)` handed back to the model as a function error, never an exception
that would crash the whole run (section 9.3: "Malformed function call ... returned to the
model as a function error so it can self-correct").
"""

import time
import uuid
from typing import Any

import jsonschema
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from relay_core.connectors.base import ExecutionContext, ToolResult
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import TOOL_FINISHED, TOOL_STARTED
from relay_core.tools.registry import BoundTool

_RETRYABLE = (TimeoutError, ConnectionError, OSError)


class ToolExecutor:
    def __init__(self, tool_calls: ToolCallRepository, events: EventPublisher) -> None:
        self.tool_calls = tool_calls
        self.events = events

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
        try:
            if bound.spec.idempotent:
                result = await self._call_with_retry(bound, ctx, args)
            else:
                result = await bound.connector.call_tool(ctx, bound.spec.name, args)
        except Exception as exc:  # noqa: BLE001 - any connector failure becomes a tool error
            result = ToolResult(ok=False, error=f"Tool call failed: {exc}")
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
