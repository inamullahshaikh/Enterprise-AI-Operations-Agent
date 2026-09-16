"""Integration test fixtures: a real Postgres via testcontainers, migrated with
the project's own Alembic revision, and an httpx client wired to the FastAPI app
with `get_session`/`get_settings_dep` overridden so every request runs inside one
rolled-back-per-test transaction against that database.

Requires a reachable Docker daemon — every test here errors (not skips) if one
isn't running, since `postgres_url`/`redis_url` fail to start their containers.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
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


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, test_settings: Settings, redis_url: str
) -> AsyncIterator[AsyncClient]:
    from relay_api.deps import get_redis, get_settings_dep
    from relay_api.main import app
    from relay_core.db.session import get_session

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    redis_client = Redis.from_url(redis_url)
    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings_dep] = lambda: test_settings
    app.dependency_overrides[get_redis] = lambda: redis_client
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        await redis_client.aclose()
