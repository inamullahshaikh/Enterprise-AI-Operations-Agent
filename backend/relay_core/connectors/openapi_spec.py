"""OpenAPI 3.x spec -> candidate tools (docs/system-design.md section 6.5 steps 1-4). Pure
functions, no I/O: the preview route fetches or receives the text, and the admin picks which of
these operations to install.

Local `$ref`s are inlined here, once, so a stored operation schema contains no refs: the
executor's `jsonschema` validation and `relay_core.tools.sanitizer` then work on it unchanged.
Swagger 2.0 is out of scope.
"""

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, computed_field

from relay_core.connectors.base import Risk

MAX_DESCRIPTION = 1000
_MAX_REF_DEPTH = 10
RISK_BY_METHOD = {
    "get": Risk.READ,
    "head": Risk.READ,
    "post": Risk.WRITE,
    "put": Risk.WRITE,
    "patch": Risk.WRITE,
    "delete": Risk.DESTRUCTIVE,
}


class Param(BaseModel):
    name: str
    location: Literal["path", "query"]


class Operation(BaseModel):
    """One candidate tool. Also the shape an `openapi` installation stores in its config, where
    `risk` is recomputed from `method` rather than trusted from whoever sent it."""

    name: str
    method: Literal["get", "head", "post", "put", "patch", "delete"]
    path: str
    description: str = ""
    input_schema: dict[str, Any]
    params: list[Param] = []
    has_body: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk(self) -> Risk:
        return RISK_BY_METHOD[self.method]


class Preview(BaseModel):
    title: str
    version: str
    servers: list[str]
    operations: list[Operation]


def parse_spec(text: str) -> dict[str, Any]:
    """YAML or JSON (JSON is valid YAML). Raises `ValueError` for anything but OpenAPI 3.x."""
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Spec is not valid YAML or JSON: {exc}") from exc
    if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3."):
        raise ValueError("Only OpenAPI 3.x specs are supported (no `openapi: 3.x` field found)")
    return spec


def preview(spec: dict[str, Any]) -> Preview:
    info = spec.get("info") or {}
    return Preview(
        title=str(info.get("title", "")),
        version=str(info.get("version", "")),
        servers=[
            str(s["url"]) for s in spec.get("servers") or [] if isinstance(s, dict) and "url" in s
        ],
        operations=operations(spec),
    )


def operations(spec: dict[str, Any]) -> list[Operation]:
    result: list[Operation] = []
    names: set[str] = set()
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method, op in item.items():
            if method not in RISK_BY_METHOD or not isinstance(op, dict):
                continue
            result.append(_operation(spec, path, method, op, shared, names))
    return result


def _operation(
    spec: dict[str, Any],
    path: str,
    method: str,
    op: dict[str, Any],
    shared: list[Any],
    names: set[str],
) -> Operation:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", op.get("operationId") or "") or (
        f"{method}_{re.sub(r'[^a-zA-Z0-9]+', '_', path).strip('_')}"
    )
    name, n = base, 1
    while name in names:
        n += 1
        name = f"{base}_{n}"
    names.add(name)

    # Operation-level parameters override path-level ones with the same name and location.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*shared, *(op.get("parameters") or [])]:
        param = inline_refs(spec, raw)
        if isinstance(param, dict) and param.get("in") in ("path", "query"):
            merged[(param["in"], param.get("name", ""))] = param

    properties: dict[str, Any] = {}
    required: list[str] = []
    for (location, param_name), param in merged.items():
        schema = dict(param.get("schema") or {"type": "string"})
        if param.get("description"):
            schema["description"] = param["description"]
        properties[param_name] = schema
        if location == "path" or param.get("required"):
            required.append(param_name)

    body = inline_refs(spec, op.get("requestBody") or {})
    body_schema = ((body.get("content") or {}).get("application/json") or {}).get("schema")
    if body_schema is not None:
        properties["body"] = body_schema
        if body.get("required"):
            required.append("body")

    description = "\n\n".join(p for p in (op.get("summary"), op.get("description")) if p)
    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    return Operation(
        name=name,
        method=method,
        path=path,
        description=description[:MAX_DESCRIPTION],
        input_schema=input_schema,
        params=[Param(name=n, location=loc) for loc, n in merged],
        has_body=body_schema is not None,
    )


def inline_refs(spec: dict[str, Any], node: Any, depth: int = 0, seen: tuple[str, ...] = ()) -> Any:
    """Replaces every local `#/...` ref with its target. A cycle, a ref deeper than
    `_MAX_REF_DEPTH`, or a ref that doesn't resolve becomes `{"type": "object"}`, the same way
    `relay_core.tools.sanitizer` gives up."""
    if isinstance(node, list):
        return [inline_refs(spec, v, depth, seen) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _lookup(spec, ref)
        if target is None or ref in seen or depth >= _MAX_REF_DEPTH:
            return {"type": "object"}
        return inline_refs(spec, target, depth + 1, (*seen, ref))
    return {k: inline_refs(spec, v, depth, seen) for k, v in node.items()}


def _lookup(spec: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
