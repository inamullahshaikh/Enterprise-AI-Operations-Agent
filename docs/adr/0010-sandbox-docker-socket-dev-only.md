# ADR-0010: The sandbox service mounts the host Docker socket — local/dev only

## Status

Accepted

## Context

`code.execute` (docs/system-design.md section 10.7) needs to run arbitrary, LLM-generated
Python in a fresh, isolated, resource-limited container per call. The sandbox service
(`sandbox/`) is itself a container, so it needs some way to launch *other* containers — the
classic "container that spawns containers" problem, with three broad answers:

1. **Mount the host's Docker socket** (`/var/run/docker.sock`) into the sandbox service, and
   have it talk to the same Docker daemon as the host via `docker-py`.
2. **Docker-outside-of-Docker on a dedicated host**, with the sandboxed containers running
   under gVisor or Kata for kernel-level isolation, on a machine with no other workload and no
   network route to Postgres/Redis.
3. **A managed sandbox provider** (e.g. a hosted code-execution API), trading control for not
   running any of this infrastructure at all.

Section 10.7's own design already flagged option 1 as a "dev-only convenience," and
`docker-compose.yml`'s `sandbox` service carried a comment to that effect before this ADR — this
document makes the reasoning explicit and records what a real deployment must do instead.

## Decision

Local development uses option 1: `docker-compose.yml`'s `sandbox` service mounts
`/var/run/docker.sock`, and `sandbox/runner.py` calls `docker-py`'s `containers.run(...)` to
launch a fresh `relay-sandbox-runtime` container per `/run` call, with `--network none`, a
read-only root filesystem, memory/CPU/pids limits, `--cap-drop ALL`, and
`--security-opt no-new-privileges` on that *inner* container.

This is a real privilege-escalation surface, not a cosmetic one: a process with access to the
Docker socket can ask the daemon to start a new container with arbitrary mounts (including the
host's root filesystem) and capabilities, which is broadly equivalent to root on the host. The
`--network none`/read-only/cap-drop flags on the *sandboxed* container don't constrain the
*sandbox service itself*, which still has full socket access. Accepting this is only reasonable
because:

- It's scoped to local/dev, where the "host" is the developer's own machine or a CI runner
  that's discarded after the job.
- It makes `code.execute` work out of the box with a single `docker compose up`, which matters
  for a portfolio project meant to be cloned and run by someone else without additional
  infrastructure setup.
- Every other credential in this environment (`LOCAL_MASTER_KEY`, the demo Postgres) is already
  a throwaway dev fixture, so the socket mount isn't introducing risk to anything that matters
  outside the container network it's already contained to.

Outputs are collected via a writable host bind mount (a per-call temp directory), not a tmpfs
read back through `get_archive()` after the container exits — that was the first implementation,
and it doesn't work: tmpfs content isn't retrievable that way once the container has stopped
(confirmed by running it, not just by reading Docker's docs). A scoped, throwaway, per-call
bind-mounted temp directory doesn't meaningfully change the threat model already accepted above.

## Consequences

- **This must not ship as-is to a shared or cloud deployment.** A production `sandbox` service
  needs option 2 or 3 instead: either Docker-outside-of-Docker with gVisor/Kata on an isolated
  host with no route to Postgres/Redis (matching docs/system-design.md section 23.1/23.2's
  reference architecture), or a managed sandbox provider. Whoever does that migration should
  treat `sandbox/runner.py`'s container-launch parameters (network/read-only/caps/limits) as the
  *minimum* bar the replacement must also meet, not the whole solution.
- `sandbox/README.md` and `docker-compose.yml`'s `sandbox` service both carry this same warning
  inline, so it's visible at the point someone would actually flip this on somewhere else, not
  only here.
- The pre-built `relay-sandbox-runtime` image (`sandbox/runtime/`) has to exist before
  `code.execute` works (`make sandbox-image`, or `make up` which depends on it) — the sandbox
  service doesn't build it on demand, so a missing image fails a `/run` call with a clear
  `RuntimeImageMissing` error rather than trying to pull or build one under load.

## Alternatives considered

- **Build gVisor/Kata support for local dev too**: rejected for now — meaningfully more setup
  complexity (a `runsc`/Kata runtime installed on the dev machine or CI runner) for a security
  property that mostly matters once multiple tenants' code can run on the same host, which local
  dev never has.
- **A hosted sandbox provider even for local dev**: rejected — it would make `code.execute`
  depend on a third-party API key just to run the eval suite or try the demo locally, which cuts
  against the same "clone and `docker compose up`" goal that motivated keeping this in-repo.
