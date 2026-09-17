.PHONY: up down logs migrate seed sync-tools test lint eval fmt sandbox-image

up: sandbox-image
	docker compose up --build

# The image code.execute's containers actually run *from* — not itself a docker-compose
# service, since nothing ever starts a container from it directly (docs/system-design.md
# section 10.7); the `sandbox` service's SANDBOX_RUNTIME_IMAGE just needs the tag to exist.
sandbox-image:
	docker build -t relay-sandbox-runtime:latest sandbox/runtime

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	docker compose exec api alembic upgrade head

seed:
	docker compose exec api python -m relay_worker.tasks.maintenance seed_demo

# Re-runs tool discovery for every active installation. An existing dev database needs this once
# to backfill tool_definitions before the registry reads from it (Phase 6 A4).
sync-tools:
	docker compose exec api python -m relay_worker.tasks.connectors

test:
	docker compose exec api pytest tests/unit
	# Integration tests use testcontainers, which needs to talk to a Docker
	# daemon directly — they run on the host (same as in CI), not inside the
	# `api` container, which deliberately has no Docker socket mounted. Requires
	# `pip install -e ".[dev]"` in backend/ on the host once.
	cd backend && pytest tests/integration
	cd frontend && npm test --if-present

lint:
	docker compose exec api ruff check .
	docker compose exec api mypy relay_core relay_api relay_worker
	cd frontend && npm run lint --if-present

fmt:
	docker compose exec api ruff format .

eval:
	docker compose exec api relay-eval run --all --ci
