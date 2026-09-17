from celery import Celery  # type: ignore[import-untyped]

from relay_core.config import get_settings

settings = get_settings()

app = Celery("relay_worker", broker=settings.redis_url, backend=settings.redis_url)

app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "relay_worker.tasks.agent.*": {"queue": "agent"},
        "relay_worker.tasks.ingest.*": {"queue": "ingest"},
        "relay_worker.tasks.memory.*": {"queue": "memory"},
        "relay_worker.tasks.connectors.*": {"queue": "connectors"},
        "relay_worker.tasks.evals.*": {"queue": "eval"},
        "relay_worker.tasks.maintenance.*": {"queue": "maintenance"},
    },
)

app.conf.beat_schedule = {
    # An undecided approval holds a run open and a checkpoint alive, so the sweep has to run on
    # its own schedule rather than piggybacking on request traffic — an abandoned workspace
    # generates none. Five minutes is fine granularity against a 24 h window
    # (`Approval.DEFAULT_EXPIRY_HOURS`); the cost is one indexed query per tick.
    "expire-stale-approvals": {
        "task": "relay_worker.tasks.maintenance.expire_stale_approvals",
        "schedule": 300.0,
    },
    # Keeps MCP tool lists fresh and catches a changed tool (section 18.1's rug-pull) within six
    # hours, without anyone pressing "sync". A health check runs first, so an installation that
    # went down and came back is picked up again too.
    "sync-connector-tools": {
        "task": "relay_worker.tasks.connectors.sync_all_installations",
        "schedule": 6 * 3600.0,
    },
    # Google access tokens live an hour. Five minutes against a fifteen-minute horizon means a
    # token is renewed well before anything reaches for it, and a revoked grant shows up on the
    # connector page within minutes instead of at the next run.
    "refresh-oauth-tokens": {
        "task": "relay_worker.tasks.connectors.refresh_oauth_tokens",
        "schedule": 300.0,
    },
}

from relay_worker.tasks import (  # noqa: E402, F401 -- registers tasks
    agent,
    connectors,
    ingest,
    maintenance,
    memory,
)

# Later phases add their task modules here, e.g.:
# from relay_worker.tasks import evals
