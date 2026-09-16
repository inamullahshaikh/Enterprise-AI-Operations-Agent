"""JSON Schema -> Gemini function-declaration `parameters` sanitizer (docs/system-design.md
section 9.2). Built-in tools already emit clean, flat schemas with no `$ref`s, so this mostly
matters once MCP/OpenAPI tools (arbitrary schemas from someone else's server/spec) exist in
Phase 6 — written and unit-tested now so both of those adapters feed through one already-proven
implementation instead of each growing its own.
"""

from typing import Any

# Keywords Gemini's function-declaration schema doesn't understand. Dropping them is safe:
# they're either purely documentary (`default`, `examples`, `$comment`) or describe constraints
# Gemini can't enforce anyway (`additionalProperties`, `patternProperties`, the `if/then/else`/
# `not`/`const` family) — the model still sees `description`/`enum`/`required` for guidance.
_UNSUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$comment",
    "default",
    "examples",
    "additionalProperties",
    "patternProperties",
    "const",
    "if",
    "then",
    "else",
    "not",
}
_MAX_DEPTH = 10


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    result = _sanitize(schema, defs=defs, depth=0)
    assert isinstance(result, dict)  # `_sanitize` always returns a dict for a dict input
    return result


def _sanitize(node: Any, *, defs: dict[str, Any], depth: int) -> Any:
    if not isinstance(node, dict):
        return node
    if depth > _MAX_DEPTH:
        # Give up gracefully on pathologically deep/self-referential schemas rather than
        # recursing until Python's own stack limit does it for us.
        return {"type": "object"}

    if "$ref" in node:
        target = _resolve_ref(node["$ref"], defs)
        if target is None:
            return {"type": "object"}
        return _sanitize(target, defs=defs, depth=depth + 1)

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_KEYWORDS or key in ("$defs", "definitions"):
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {k: _sanitize(v, defs=defs, depth=depth + 1) for k, v in value.items()}
        elif key == "items":
            result[key] = _sanitize(value, defs=defs, depth=depth + 1)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            result[key] = [_sanitize(v, defs=defs, depth=depth + 1) for v in value]
        else:
            result[key] = value
    return result


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return defs.get(ref[len(prefix) :])
    return None
