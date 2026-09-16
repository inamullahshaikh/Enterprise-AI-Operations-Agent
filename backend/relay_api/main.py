from fastapi import FastAPI
from sqlalchemy import text

from relay_api.errors import install_error_handlers
from relay_api.routers import auth, debug, workspaces
from relay_core.db.session import get_engine

app = FastAPI(
    title="Relay API",
    version="0.1.0",
    description="Relay — Enterprise AI Operations Agent",
)
install_error_handlers(app)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(workspaces.router, prefix="/api/v1")
# debug.router gates itself out in prod (see its `require_non_prod` dependency)
# rather than being conditionally mounted here, so settings are only ever read
# per-request, never at import time.
app.include_router(debug.router, prefix="/api/v1")


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    """Liveness probe: process is up and can serve requests."""
    return {"status": "ok"}


@app.get("/readyz", tags=["meta"])
async def readyz() -> dict[str, str]:
    """Readiness probe: the API can actually reach Postgres."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
