from fastapi import FastAPI

from relay_core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Relay API",
    version="0.1.0",
    description="Relay — Enterprise AI Operations Agent",
)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    """Liveness probe: process is up and can serve requests."""
    return {"status": "ok"}


@app.get("/readyz", tags=["meta"])
async def readyz() -> dict[str, str]:
    """Readiness probe. Phase 1 will add real DB/Redis connectivity checks here."""
    return {"status": "ok"}


# Phase 1+ routers are mounted here as they land, e.g.:
# from relay_api.routers import auth, workspaces
# app.include_router(auth.router, prefix="/api/v1")
# app.include_router(workspaces.router, prefix="/api/v1")
