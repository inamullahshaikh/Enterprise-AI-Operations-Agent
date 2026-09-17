FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies come from pyproject alone, so this layer is only invalidated when the
# dependency list changes — not when any source file does. The package directories are created
# empty purely so the editable install has somewhere to point; the real code arrives in the
# COPY below, and in dev from the bind mount in docker-compose.yml. Copying the source *before*
# installing is what made `make up` re-download every dependency after every edit.
COPY backend/pyproject.toml ./
RUN mkdir -p relay_core relay_api relay_worker \
    && touch relay_core/__init__.py relay_api/__init__.py relay_worker/__init__.py

# The pip cache is a BuildKit cache mount rather than image content: it survives across builds
# (so a changed dependency list re-installs from disk instead of the network) without landing
# in a layer.
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[dev]"

# The eval harness (docs/system-design.md section 21) is a separate installable package —
# see evals/README.md — but needs relay_core/relay_api importable to run real graph invocations,
# so it's installed into the same image rather than shipped separately. This is why the build
# context is the repo root (docker-compose.yml), not just backend/.
COPY evals/relay_eval /evals/relay_eval
RUN --mount=type=cache,target=/root/.cache/pip pip install -e /evals/relay_eval

COPY backend/. .

EXPOSE 8000

CMD ["uvicorn", "relay_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
