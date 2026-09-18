"""PII redaction for tool output that reaches the model (docs/system-design.md section 20.4,
`workspace_policies.pii_redaction`).

Email addresses and phone numbers become stable placeholders (`<email:1>`, `<phone:1>`). The
placeholder-to-value mapping lives on run state, so the same address is always the same
placeholder within a run, and `restore` puts the real values back where they are needed: in a
tool's arguments before they reach the connector (`ToolExecutor`), and in the answer the user
reads (`validate_final`, `finalize`). The model never sees the address; the connector and the
user always do. That includes the email allow-list, which runs inside the connector and so
checks the real domain.

Names and postal addresses are not covered: that is entity recognition, not a regex.
"""

import json
import re
from functools import partial
from typing import Any

_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # International or North American shapes with at least 9 digits, so years, amounts and
    # order numbers are left alone.
    "phone": re.compile(r"(?<![\w+])\+?\d(?:[\s().-]*\d){8,14}(?!\w)"),
}
_PLACEHOLDER = re.compile(r"<(?:email|phone):\d+>")


def redact(value: Any, mapping: dict[str, str]) -> Any:
    """Returns `value` (any JSON-shaped data) with PII replaced. `mapping` (placeholder -> real
    value) is extended in place with any new placeholders."""
    reverse = {real: ph for ph, real in mapping.items()}

    def _sub(kind: str, match: re.Match[str]) -> str:
        real = match.group(0)
        if real not in reverse:
            n = sum(1 for ph in mapping if ph.startswith(f"<{kind}:")) + 1
            reverse[real] = placeholder = f"<{kind}:{n}>"
            mapping[placeholder] = real
        return reverse[real]

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            for kind, pattern in _PATTERNS.items():
                v = pattern.sub(partial(_sub, kind), v)
            return v
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [_walk(x) for x in v]
        return v  # numbers stay numbers: a phone stored as an int is not redacted

    return _walk(json.loads(json.dumps(value, default=str)))


def restore(value: Any, mapping: dict[str, str]) -> Any:
    """Puts real values back into strings anywhere inside `value`."""
    if not mapping:
        return value
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, dict):
        return {k: restore(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [restore(v, mapping) for v in value]
    return value
