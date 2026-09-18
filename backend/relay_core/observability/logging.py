"""JSON logging with secrets scrubbed and `request_id`/`run_id` on every line
(docs/system-design.md sections 20.1, 20.4).

The rest of the codebase logs through the standard library, so structlog is installed as the
root handler's formatter rather than asking every module to switch. Context comes from
`structlog.contextvars`: the API middleware binds `request_id`, the agent runner binds `run_id`.
This is the one piece of section 20 that survives ADR-0008 without a tracing backend.
"""

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

from relay_core.security.scrub import scrub


def _scrub(_logger: Any, _method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    scrubbed: MutableMapping[str, Any] = scrub(dict(event))
    return scrubbed


_SHARED: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.format_exc_info,
    _scrub,
]


def configure_logging(level: str = "INFO") -> None:
    """Idempotent; the API and the worker each call it once at startup."""
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
