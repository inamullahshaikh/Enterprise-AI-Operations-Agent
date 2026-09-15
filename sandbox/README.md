# Relay Sandbox

Ephemeral Python execution service backing the `code.execute` capability
(`python_sandbox` connector). See docs/system-design.md section 10.7 and section 23 for the
full design: fresh container per execution, `--network none`, resource limits,
gVisor where available, and a completely separate host with no route to
Postgres/Redis in the cloud deployment.

Implemented in Phase 4.
