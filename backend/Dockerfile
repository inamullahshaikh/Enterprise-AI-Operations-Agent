FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml ./
COPY backend/relay_core ./relay_core
COPY backend/relay_api ./relay_api
COPY backend/relay_worker ./relay_worker

RUN pip install --no-cache-dir -e ".[dev]"

# The eval harness (docs/system-design.md section 21) is a separate installable package —
# see evals/README.md — but needs relay_core/relay_api importable to run real graph invocations,
# so it's installed into the same image rather than shipped separately. This is why the build
# context is the repo root (docker-compose.yml), not just backend/.
COPY evals/relay_eval /evals/relay_eval
RUN pip install --no-cache-dir -e /evals/relay_eval

COPY backend/. .

EXPOSE 8000

CMD ["uvicorn", "relay_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
