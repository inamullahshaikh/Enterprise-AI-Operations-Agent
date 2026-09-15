from fastapi import FastAPI

app = FastAPI(title="Relay Mock Services")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Phase 5 (docs/system-design.md section 21.2) adds deterministic fake HubSpot,
# Gmail, Calendar, and web-search endpoints here, used for local dev and for
# reproducible, free eval runs.
