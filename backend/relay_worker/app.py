from celery import Celery

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

# Phase 2+ tasks are registered here as they land, e.g.:
# from relay_worker.tasks import agent, ingest, memory, connectors, maintenance, evals
