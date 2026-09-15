.PHONY: up down logs migrate seed test lint eval fmt

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	docker compose exec api alembic upgrade head

seed:
	docker compose exec api python -m relay_worker.tasks.maintenance seed_demo

test:
	docker compose exec api pytest
	cd frontend && npm test --if-present

lint:
	docker compose exec api ruff check .
	docker compose exec api mypy relay_core relay_api relay_worker
	cd frontend && npm run lint --if-present

fmt:
	docker compose exec api ruff format .

eval:
	docker compose exec api relay-eval run --all --ci
