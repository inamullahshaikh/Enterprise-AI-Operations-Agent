"""Integration test fixtures: a real Postgres via testcontainers, migrated with
the project's own Alembic revision, and an httpx client wired to the FastAPI app
with `get_session`/`get_settings_dep` overridden so every request runs inside one
rolled-back-per-test transaction against that database.

Requires a reachable Docker daemon — every test here errors (not skips) if one
isn't running, since `postgres_url`/`redis_url` fail to start their containers.
"""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from alembic import command
from relay_core.config import Settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def postgres_url() -> str:
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> str:
    with RedisContainer() as rc:
        yield f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def migrated_db_url(postgres_url: str) -> str:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    command.upgrade(cfg, "head")
    return postgres_url


@pytest_asyncio.fixture
async def db_session(migrated_db_url: str) -> AsyncIterator[AsyncSession]:
    """One connection/transaction per test, rolled back at teardown. The
    `create_savepoint` join mode means the app's own `session.commit()` (inside
    the real `get_session()` dependency, which we bypass) isn't needed for
    writes made through this session to be visible to later reads in the same
    test — a plain `flush()` is enough because everything shares one transaction.
    """
    engine = create_async_engine(migrated_db_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


class _UnavailableModels:
    async def generate_content(self, **_: object) -> object:
        raise RuntimeError("Gemini is not available in integration tests")

    async def embed_content(self, **_: object) -> object:
        raise RuntimeError("Gemini is not available in integration tests")


class UnavailableGenaiClient:
    def __init__(self) -> None:
        self.aio = type("_Aio", (), {"models": _UnavailableModels()})()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, test_settings: Settings, redis_url: str
) -> AsyncIterator[AsyncClient]:
    from relay_api.deps import get_genai_client, get_redis, get_settings_dep
    from relay_api.main import app
    from relay_core.db.session import get_session

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    redis_client = Redis.from_url(redis_url)
    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings_dep] = lambda: test_settings
    app.dependency_overrides[get_redis] = lambda: redis_client
    # Installing a connector tags and embeds its tools through the gateway. No test reaches real
    # Gemini: this client fails every call, which sync treats as "untagged, unembedded". A test
    # that scripts Gemini overrides this dependency itself.
    app.dependency_overrides[get_genai_client] = lambda: UnavailableGenaiClient()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        await redis_client.aclose()


# `mocks/` and `mcp_examples/` are separate container images, not packages the backend imports,
# so they load by path.
_MOCKS_MAIN = _BACKEND_ROOT.parent / "mocks" / "main.py"
_MCP_TICKETING = _BACKEND_ROOT.parent / "mcp_examples" / "ticketing" / "server.py"


def load_module_by_path(name: str, path: Path):
    """A freshly executed module each call, so each test gets freshly seeded in-memory state."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def serve_asgi(app) -> AsyncIterator[str]:
    """Function-scoped by design: pytest-asyncio gives each test its own event loop, and a server
    started on a module-scoped loop would sit there un-driven while the tests run."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:  # uvicorn exposes no awaitable "ready" signal
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest_asyncio.fixture
async def mock_services_url() -> AsyncIterator[str]:
    """A freshly seeded inbox and calendar per test, with no `/_reset` needed between them."""
    async with serve_asgi(load_module_by_path("relay_mocks_main", _MOCKS_MAIN).app) as url:
        yield url


@pytest_asyncio.fixture
async def mcp_ticketing() -> AsyncIterator[tuple[str, object]]:
    """B1's sample MCP server in-process: `(base_url, module)`. The MCP endpoint is
    `{base_url}/mcp`; the module is there for a test that needs `build_app()` with a token."""
    module = load_module_by_path("relay_mcp_ticketing", _MCP_TICKETING)
    async with serve_asgi(module.app) as url:
        yield url, module


@pytest.fixture
def ssrf_allows_localhost(monkeypatch: pytest.MonkeyPatch, test_settings: Settings) -> None:
    """The SSRF guard reads `get_settings()` (connectors are built with no arguments), and test
    servers live on 127.0.0.1, which it blocks unless allow-listed."""
    from relay_core.security import ssrf

    patched = test_settings.model_copy(update={"env": "dev", "ssrf_allowed_hosts": ["127.0.0.1"]})
    monkeypatch.setattr(ssrf, "get_settings", lambda: patched)
