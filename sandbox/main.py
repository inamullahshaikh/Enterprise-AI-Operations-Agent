import asyncio

from fastapi import FastAPI
from pydantic import BaseModel, Field

from runner import RunResult, RuntimeImageMissing
from runner import run as run_in_container

app = FastAPI(title="Relay Sandbox")

_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 120.0


class RunRequest(BaseModel):
    code: str
    inputs: dict = Field(default_factory=dict)
    timeout_s: float = _DEFAULT_TIMEOUT_S


class RunResponse(BaseModel):
    ok: bool
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    error: str | None
    files: dict[str, str]
    truncated: bool

    @classmethod
    def from_result(cls, result: RunResult) -> "RunResponse":
        return cls(
            ok=result.ok,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            error=result.error,
            files=result.files,
            truncated=result.truncated,
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=RunResponse)
async def run_code(body: RunRequest) -> RunResponse:
    timeout_s = min(body.timeout_s, _MAX_TIMEOUT_S)
    try:
        result = await asyncio.to_thread(
            run_in_container, code=body.code, inputs=body.inputs, timeout_s=timeout_s
        )
    except RuntimeImageMissing as exc:
        return RunResponse.from_result(
            RunResult(ok=False, error=str(exc), exit_code=None)
        )
    return RunResponse.from_result(result)
