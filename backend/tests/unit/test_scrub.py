"""Phase 8 C3: secret scrubbing for logs and audit rows, and PII placeholders."""

import json
import logging

from relay_core.observability.logging import configure_logging
from relay_core.security.pii import redact, restore
from relay_core.security.scrub import REDACTED, scrub


def test_secret_keys_are_redacted_at_any_depth() -> None:
    value = {"a": [{"b": {"authorization": "Bearer abc.def"}}], "refresh_token": "r", "n": 1}
    assert scrub(value) == {
        "a": [{"b": {"authorization": REDACTED}}],
        "refresh_token": REDACTED,
        "n": 1,
    }


def test_a_key_shaped_string_is_redacted_under_an_innocuous_key() -> None:
    out = scrub({"note": "use sk-abcdefghijklmnopqrstu to call it", "word": "password reset"})
    assert out == {"note": f"use {REDACTED} to call it", "word": "password reset"}


def test_log_lines_are_json_and_scrubbed(capsys) -> None:
    configure_logging()
    logging.getLogger("t").warning("calling with Bearer eyJabcdefghijk.eyJabcdefghijk.sig")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["level"] == "warning"
    assert "eyJ" not in line["event"] and REDACTED in line["event"]
    logging.getLogger().handlers.clear()


def test_pii_placeholders_are_stable_and_restorable() -> None:
    mapping: dict[str, str] = {}
    out = redact(
        {"rows": [{"email": "a@acme.com", "cc": "a@acme.com", "tel": "+1 415 555 0100", "n": 5}]},
        mapping,
    )
    assert out == {"rows": [{"email": "<email:1>", "cc": "<email:1>", "tel": "<phone:1>", "n": 5}]}
    assert restore({"to": ["<email:1>"], "body": "call <phone:1>"}, mapping) == {
        "to": ["a@acme.com"],
        "body": "call +1 415 555 0100",
    }
    redact("b@acme.com", mapping)
    assert mapping["<email:2>"] == "b@acme.com"
