"""Coverage for `relay_core.connectors.manifest` (docs/system-design.md section 6.3): the
catalog Phase 3/4 use in place of a `connector_definitions` table (docs/adr/0009). Nothing
exercised this module directly before — `GET /connectors/catalog` and the tool registry both
call through it, but always indirectly.
"""

from relay_core.connectors.base import AuthType
from relay_core.connectors.manifest import get_manifest, load_manifests


def test_catalog_contains_every_builtin() -> None:
    assert set(load_manifests()) == {"postgres", "file_upload", "documents", "python_sandbox"}


def test_postgres_manifest_matches_the_connector_and_its_config_needs() -> None:
    manifest = get_manifest("postgres")
    assert manifest is not None
    assert manifest.auth_type == AuthType.CONNECTION_STRING
    assert manifest.provides_capabilities == [
        "sql.query",
        "customer.read",
        "subscription.read",
        "usage.read",
    ]
    assert manifest.config_schema["required"] == ["host", "port", "database"]
    assert manifest.secrets_schema["required"] == ["username", "password"]


def test_file_upload_manifest_has_no_config_or_secrets() -> None:
    manifest = get_manifest("file_upload")
    assert manifest is not None
    assert manifest.auth_type == AuthType.NONE
    assert manifest.config_schema == {}
    assert manifest.secrets_schema == {}
    assert manifest.provides_capabilities == ["file.read"]


def test_documents_manifest_has_no_config_or_secrets() -> None:
    manifest = get_manifest("documents")
    assert manifest is not None
    assert manifest.auth_type == AuthType.NONE
    assert manifest.config_schema == {}
    assert manifest.secrets_schema == {}
    assert manifest.provides_capabilities == ["knowledge.search"]


def test_python_sandbox_manifest_has_no_config_or_secrets() -> None:
    manifest = get_manifest("python_sandbox")
    assert manifest is not None
    assert manifest.auth_type == AuthType.NONE
    assert manifest.config_schema == {}
    assert manifest.secrets_schema == {}
    assert manifest.provides_capabilities == ["code.execute"]


def test_unknown_key_returns_none_not_a_raise() -> None:
    assert get_manifest("not-a-real-connector") is None


def test_load_manifests_is_cached_across_calls() -> None:
    # `lru_cache` — the catalog is loaded from disk once at import time, per docs/adr/0009's
    # "no dynamic tool discovery" decision. Same dict object back on every call.
    assert load_manifests() is load_manifests()
