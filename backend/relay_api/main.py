import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from relay_api.errors import install_error_handlers
from relay_api.routers import (
    approvals,
    auth,
    connectors,
    conversations,
    debug,
    documents,
    memories,
    oauth,
    runs,
    tools,
    workspaces,
)
from relay_core.db.repositories.audit import client_ip
from relay_core.db.session import get_engine
from relay_core.observability.logging import configure_logging

configure_logging()

app = FastAPI(
    title="Relay API",
    version="0.1.0",
    description="Relay — Enterprise AI Operations Agent",
)
install_error_handlers(app)


@app.middleware("http")
async def _bind_request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Binds what every log line and audit row of this request should carry: a `request_id`
    (the caller's `X-Request-ID` if it sent one, echoed back) and the client address."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    # ponytail: direct peer address; honour X-Forwarded-For once a trusted proxy sits in front.
    client_ip.set(request.client.host if request.client else None)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(auth.router, prefix="/api/v1")
app.include_router(workspaces.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(connectors.router, prefix="/api/v1")
app.include_router(connectors.catalog_router, prefix="/api/v1")
app.include_router(oauth.router, prefix="/api/v1")
app.include_router(oauth.callback_router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(memories.router, prefix="/api/v1")
app.include_router(approvals.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
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
