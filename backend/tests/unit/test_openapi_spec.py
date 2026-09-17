"""OpenAPI spec preview (Phase 6 B4): pure parsing, no I/O."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from relay_core.connectors.base import Risk
from relay_core.connectors.openapi_spec import parse_spec, preview

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Accounts", "version": "2.1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "schemas": {
            "Account": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "parent": {"$ref": "#/components/schemas/Account"},
                },
            },
        },
        "parameters": {"Limit": {"name": "limit", "in": "query", "schema": {"type": "integer"}}},
    },
    "paths": {
        "/accounts/{account_id}": {
            "parameters": [
                {"name": "account_id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "operationId": "get-account",
                "summary": "Get an account",
                "description": "x" * 2000,
                "parameters": [
                    {"$ref": "#/components/parameters/Limit"},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
            },
            "put": {
                "operationId": "get-account",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Account"}}
                    },
                },
            },
            "delete": {},
        },
    },
}


def test_operations_are_named_risked_and_merged() -> None:
    result = preview(parse_spec(yaml.safe_dump(_SPEC)))

    assert (result.title, result.version, result.servers) == (
        "Accounts",
        "2.1",
        ["https://api.example.com/v1"],
    )
    ops = {op.name: op for op in result.operations}
    assert set(ops) == {"get_account", "get_account_2", "delete_accounts_account_id"}

    get = ops["get_account"]
    assert get.risk is Risk.READ
    assert get.description.startswith("Get an account\n\n") and len(get.description) == 1000
    assert get.input_schema["properties"].keys() == {"account_id", "limit"}  # header skipped
    assert get.input_schema["required"] == ["account_id"]
    assert [(p.name, p.location) for p in get.params] == [
        ("account_id", "path"),
        ("limit", "query"),
    ]
    assert get.has_body is False

    put = ops["get_account_2"]
    assert put.risk is Risk.WRITE and put.has_body
    assert put.input_schema["required"] == ["account_id", "body"]
    account = put.input_schema["properties"]["body"]
    assert account["properties"]["name"] == {"type": "string"}
    # The recursive ref becomes a plain object instead of recursing forever.
    assert account["properties"]["parent"] == {"type": "object"}
    assert "$ref" not in json.dumps(put.input_schema)

    assert ops["delete_accounts_account_id"].risk is Risk.DESTRUCTIVE


def test_swagger_2_is_rejected() -> None:
    with pytest.raises(ValueError, match="OpenAPI 3"):
        parse_spec(json.dumps({"swagger": "2.0", "paths": {}}))


def test_the_mock_services_own_fastapi_spec_parses() -> None:
    """FastAPI emits OpenAPI 3.1."""
    path = Path(__file__).resolve().parents[3] / "mocks" / "main.py"
    spec = importlib.util.spec_from_file_location("relay_mocks_main_openapi", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = preview(parse_spec(json.dumps(module.app.openapi())))

    ops = {op.path: op for op in result.operations if op.method == "get"}
    assert ops["/gmail/messages/{message_id}"].input_schema["required"] == ["message_id"]
    assert any(op.has_body and op.risk is Risk.WRITE for op in result.operations)
