# Relay Sandbox

Ephemeral Python execution service backing the `code.execute` capability
(`relay_core.connectors.builtin.python_sandbox.PythonSandboxConnector`). See
docs/system-design.md section 10.7 for the full design.

## How it works

- `main.py` is the long-running FastAPI service (`POST /run`, `GET /healthz`) the backend talks
  to over HTTP (`settings.sandbox_url`).
- `runner.py` is what actually executes code: for every `/run` call it launches a **fresh
  container** from the pre-built `relay-sandbox-runtime` image (build it once with
  `make sandbox-image`, or `docker build -t relay-sandbox-runtime:latest runtime/`), with
  `--network none`, a read-only root filesystem, a small writable tmpfs at `/work/outputs`,
  memory/CPU/pids limits, `--cap-drop ALL`, and `--security-opt no-new-privileges`. The
  container is killed and removed even on a timeout.
- `runtime/` is that pre-built image's own build context: `python:3.12-slim` plus
  pandas/numpy/scipy/matplotlib/scikit-learn, and `entrypoint.py` (baked into the image, never
  user-controlled) which loads the caller's `inputs` dict and `exec`s their code with the
  working directory set to the writable `/work/outputs` tmpfs.

## Security note

This service is given access to the host's Docker socket (`docker-compose.yml`'s `sandbox`
service) so it can launch those per-execution containers — that access is **equivalent to root
on the host**, since a container with the Docker socket can launch siblings with arbitrary
mounts and capabilities. That's a deliberate, **local/dev-only** shortcut so `code.execute`
works out of the box with `docker compose up`, not something to carry into a shared or cloud
deployment. There, per docs/system-design.md section 23.1/23.2, use a managed sandbox provider
or Docker-outside-of-Docker with gVisor/Kata, on a host with no other sensitive workloads and no
route to Postgres/Redis.

## Inputs and `ref://` handles

The `python_sandbox` connector resolves `ref://tool_call/<id>` values in its `inputs` argument
into the referenced tool call's full (untruncated) output before calling this service — see that
connector's docstring. This service itself only ever sees already-resolved JSON values; it has
no database access of its own.
