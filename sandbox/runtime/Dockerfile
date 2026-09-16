# The per-execution image (docs/system-design.md section 10.7): python:3.12-slim + the
# analysis stack `run_python` promises. Built and tagged once (`docker build -t
# relay-sandbox-runtime:latest .`, or via docker-compose — see ../README.md), then reused as
# the image every ephemeral container is created from; it is never itself the long-running
# service (that's ../Dockerfile).
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    pandas>=2.2 \
    numpy>=1.26 \
    scipy>=1.13 \
    matplotlib>=3.9 \
    scikit-learn>=1.5 \
    && useradd --create-home --uid 1000 sandboxuser

COPY entrypoint.py /opt/entrypoint.py

# Both point at /work/outputs (the tmpfs runner.py mounts writable) rather than the sandbox
# user's home directory, which sits on the container's --read-only root filesystem: matplotlib
# and Python's own bytecode cache would otherwise fail to write on first import.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/work/outputs/.mplconfig \
    PYTHONDONTWRITEBYTECODE=1

USER sandboxuser
WORKDIR /work

ENTRYPOINT ["python", "/opt/entrypoint.py"]
