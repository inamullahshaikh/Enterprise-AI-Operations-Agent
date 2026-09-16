"""Connector manifests (docs/system-design.md section 6.3): static, human-edited metadata
about each built-in connector *type* — display name, category, auth type, config/secrets JSON
Schema, and which capabilities it provides. Per docs/adr/0009, there's no `connector_definitions`
table yet; this module is the whole catalog, loaded from YAML once at import time.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from relay_core.connectors.base import AuthType

_MANIFEST_DIR = Path(__file__).parent / "manifests"


class ConnectorManifest(BaseModel):
    key: str
    display_name: str
    category: str
    description: str
    auth_type: AuthType
    config_schema: dict[str, Any] = {}
    secrets_schema: dict[str, Any] = {}
    provides_capabilities: list[str] = []
    docs_url: str | None = None


@lru_cache
def load_manifests() -> dict[str, ConnectorManifest]:
    manifests: dict[str, ConnectorManifest] = {}
    for path in sorted(_MANIFEST_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        manifest = ConnectorManifest.model_validate(raw)
        manifests[manifest.key] = manifest
    return manifests


def get_manifest(key: str) -> ConnectorManifest | None:
    return load_manifests().get(key)
