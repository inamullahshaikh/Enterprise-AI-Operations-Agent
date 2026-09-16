"""Container-per-execution orchestration (docs/system-design.md section 10.7). This process
talks to the Docker daemon over the socket mounted into its own container to launch a fresh,
network-isolated, resource-limited container per `run()` call.

**Security note, not a detail to gloss over**: mounting the host's Docker socket into this
service is a real privilege-escalation surface — a container with Docker socket access can
launch siblings with arbitrary mounts/capabilities, which is broadly equivalent to root on the
host. It is a deliberate, documented **local/dev-only** shortcut so `code.execute` works out of
the box with `docker compose up` (see docs/system-design.md section 23.1/23.2 and this
directory's README). A shared or cloud deployment must not reuse this: use a managed sandbox
provider, or Docker-outside-of-Docker with gVisor/Kata, running on a host with no other
sensitive workloads.

Outputs are collected via a writable host bind mount, not a tmpfs read back through
`get_archive()` after the container exits: tmpfs content isn't retrievable that way — the mount
is torn down with the container's runtime state before `get_archive()` can read it (confirmed
empirically, not just from docs). A scoped writable temp directory doesn't meaningfully change
this service's threat model beyond what mounting the Docker socket already accepts.
"""

import base64
import json
import os
import tempfile
from dataclasses import dataclass, field

import docker
from docker.errors import APIError, ImageNotFound
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

RUNTIME_IMAGE = os.environ.get("SANDBOX_RUNTIME_IMAGE", "relay-sandbox-runtime:latest")

_MEMORY_LIMIT = "512m"
_NANO_CPUS = 1_000_000_000  # 1 CPU
_PIDS_LIMIT = 128
_MAX_OUTPUT_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 20 * 1024 * 1024
_LOG_TRUNCATE_CHARS = 20_000
_IGNORED_OUTPUT_PREFIXES = (".mplconfig",)


class RuntimeImageMissing(Exception):
    pass


@dataclass
class RunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    # {filename: base64-encoded content}
    files: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


def run(*, code: str, inputs: dict, timeout_s: float) -> RunResult:
    client = docker.from_env()
    try:
        client.images.get(RUNTIME_IMAGE)
    except ImageNotFound as exc:
        raise RuntimeImageMissing(
            f"{RUNTIME_IMAGE!r} is not built — run `docker build -t {RUNTIME_IMAGE} runtime/` "
            "(see sandbox/README.md)"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="relay-sandbox-") as base_dir:
        inputs_dir = os.path.join(base_dir, "inputs")
        outputs_dir = os.path.join(base_dir, "outputs")
        os.makedirs(inputs_dir)
        os.makedirs(outputs_dir)
        # The runtime container writes here as its own non-root user (uid 1000, `sandboxuser`
        # in runtime/Dockerfile), which won't generally match whatever user this service's own
        # process runs as — world-writable is safe here since the directory is a throwaway
        # per-call temp dir on an isolated path, not anything sensitive.
        os.chmod(outputs_dir, 0o777)
        with open(os.path.join(inputs_dir, "code.py"), "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(inputs_dir, "inputs.json"), "w", encoding="utf-8") as f:
            json.dump(inputs, f, default=str)

        container = client.containers.run(
            RUNTIME_IMAGE,
            detach=True,
            network_mode="none",
            read_only=True,
            volumes={
                inputs_dir: {"bind": "/work/inputs", "mode": "ro"},
                outputs_dir: {"bind": "/work/outputs", "mode": "rw"},
            },
            mem_limit=_MEMORY_LIMIT,
            nano_cpus=_NANO_CPUS,
            pids_limit=_PIDS_LIMIT,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
        try:
            timed_out = False
            try:
                status = container.wait(timeout=timeout_s)
                exit_code = status.get("StatusCode")
            except (ReadTimeout, RequestsConnectionError):
                # docker-py's Windows (named-pipe) transport wraps a read timeout in a bare
                # ConnectionError rather than raising ReadTimeout directly the way the Unix
                # socket transport this service actually runs over (Linux, /var/run/docker.sock)
                # does — caught here too so local dev on Windows behaves the same way.
                timed_out = True
                exit_code = None
                try:
                    container.kill()
                except APIError:
                    pass  # already exited between the timeout and the kill

            stdout = _decode_logs(container, stdout=True, stderr=False)
            stderr = _decode_logs(container, stdout=False, stderr=True)
            files, truncated = _collect_outputs(outputs_dir)

            return RunResult(
                ok=(not timed_out and exit_code == 0),
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                timed_out=timed_out,
                error="Execution timed out" if timed_out else None,
                files=files,
                truncated=truncated,
            )
        finally:
            container.remove(force=True)


def _decode_logs(container, *, stdout: bool, stderr: bool) -> str:
    raw = container.logs(stdout=stdout, stderr=stderr, stream=False)
    text = raw.decode("utf-8", errors="replace")
    if len(text) > _LOG_TRUNCATE_CHARS:
        text = text[:_LOG_TRUNCATE_CHARS] + "\n... (truncated)"
    return text


def _collect_outputs(outputs_dir: str) -> tuple[dict[str, str], bool]:
    files: dict[str, str] = {}
    total_bytes = 0
    truncated = False
    for root, _dirs, filenames in os.walk(outputs_dir):
        for filename in filenames:
            rel_dir = os.path.relpath(root, outputs_dir)
            rel_path = filename if rel_dir == "." else os.path.join(rel_dir, filename)
            if rel_path.startswith(_IGNORED_OUTPUT_PREFIXES):
                continue
            full_path = os.path.join(root, filename)
            size = os.path.getsize(full_path)
            if size > _MAX_OUTPUT_FILE_BYTES or total_bytes + size > _MAX_TOTAL_OUTPUT_BYTES:
                truncated = True
                continue
            with open(full_path, "rb") as f:
                data = f.read()
            files[rel_path.replace(os.sep, "/")] = base64.b64encode(data).decode("ascii")
            total_bytes += size
    return files, truncated
