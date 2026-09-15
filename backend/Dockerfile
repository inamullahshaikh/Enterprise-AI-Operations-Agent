FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY relay_core ./relay_core
COPY relay_api ./relay_api
COPY relay_worker ./relay_worker

RUN pip install --no-cache-dir -e ".[dev]"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "relay_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
