"""Secret redaction for anything persisted or logged (docs/system-design.md section 20.4).

`audit_logs.details` goes through it (`AuditLogRepository.record`), and so does every log line
(`relay_core.observability.logging`). Values are redacted when their key names a secret, or when
the string itself has a credential's shape, using the memory extractor's patterns so the two
cannot drift. A bearer token in an `Authorization` header is caught by both.
"""

import re
from typing import Any

from relay_core.memory.extract import SECRET_VALUE_SHAPES

_SECRET_SHAPES = re.compile(rf"(?:\bBearer\s+\S+|{SECRET_VALUE_SHAPES})")

REDACTED = "[REDACTED]"
_SECRET_KEYS = (
    "password",
    "token",
    "authorization",
    "secret",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "refresh_token",
    "client_secret",
)


def scrub(value: Any) -> Any:
    """Returns a copy of `value` with secret-named keys and secret-shaped strings redacted,
    at any nesting depth."""
    if isinstance(value, dict):
        return {
            k: REDACTED if any(s in str(k).lower() for s in _SECRET_KEYS) else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_SHAPES.sub(REDACTED, value)
    return value
