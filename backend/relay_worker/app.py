from celery import Celery  # type: ignore[import-untyped]

from relay_core.config import get_settings
from relay_core.observability.logging import configure_logging

settings = get_settings()
configure_logging()

app = Celery("relay_worker", broker=settings.redis_url, backend=settings.redis_url)

app.conf.update(
    task_acks_late=True,
    # Keep `configure_logging`'s JSON handler instead of Celery's own.
    worker_hijack_root_logger=False,
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
    # Section 19.1: a run with no LLM or tool activity for ten minutes is failed as `stalled`.
    "fail-stalled-runs": {
        "task": "relay_worker.tasks.maintenance.fail_stalled_runs",
        "schedule": 300.0,
    },
    # Section 14.5, against each workspace's `data_retention_days`.
    "apply-retention": {
        "task": "relay_worker.tasks.maintenance.apply_retention",
        "schedule": 24 * 3600.0,
    },
    # Keeps MCP tool lists fresh and catches a changed tool (section 18.1's rug-pull) within six
    # hours, without anyone pressing "sync". A health check runs first, so an installation that
    # went down and came back is picked up again too.
    "sync-connector-tools": {
        "task": "relay_worker.tasks.connectors.sync_all_installations",
        "schedule": 6 * 3600.0,
    },
    # An installation that is already degraded or down (including one a circuit breaker took
    # out, section 19.1) is re-checked every ten minutes, so recovery is noticed in minutes
    # rather than at the next six-hourly sweep. Only unhealthy rows are visited, so this is one
    # indexed query per tick on a healthy deployment.
    "recheck-unhealthy-connectors": {
        "task": "relay_worker.tasks.connectors.recheck_unhealthy_installations",
        "schedule": 600.0,
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
