from relay_core.tools.sanitizer import sanitize_schema


def test_drops_unsupported_keywords() -> None:
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "default": "x", "additionalProperties": False},
        },
    }
    out = sanitize_schema(schema)
    assert "default" not in out["properties"]["note"]
    assert "additionalProperties" not in out["properties"]["note"]


def test_resolves_a_local_ref() -> None:
    schema = {
        "type": "object",
        "properties": {"account": {"$ref": "#/$defs/Account"}},
        "$defs": {"Account": {"type": "object", "properties": {"id": {"type": "string"}}}},
    }
    out = sanitize_schema(schema)
    assert out["properties"]["account"] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    assert "$defs" not in out


def test_unresolvable_ref_falls_back_to_a_bare_object() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}}
    out = sanitize_schema(schema)
    assert out["properties"]["x"] == {"type": "object"}


def test_sanitizes_nested_items_and_anyof() -> None:
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string", "default": "x"}},
            "value": {"anyOf": [{"type": "string", "default": "x"}, {"type": "integer"}]},
        },
    }
    out = sanitize_schema(schema)
    assert out["properties"]["tags"]["items"] == {"type": "string"}
    assert out["properties"]["value"]["anyOf"][0] == {"type": "string"}
