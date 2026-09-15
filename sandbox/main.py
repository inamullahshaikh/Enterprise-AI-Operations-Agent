from fastapi import FastAPI

app = FastAPI(title="Relay Sandbox")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Phase 4 (docs/system-design.md section 10.7 / 23) implements POST /run:
# receives {code, inputs, timeout}, executes in a fresh, network-isolated,
# resource-limited container per call, and returns stdout/stderr + artifacts.
