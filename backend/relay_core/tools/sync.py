"""Tool discovery sync (docs/system-design.md sections 6.4 "Discovery" and 17.3): turns one
installation's `connector.list_tools(ctx)` into its `tool_definitions` rows. Every installed
connector goes through here, built-ins included, so review state and admin overrides live in one
place whatever a tool's source.

Upsert rules: `is_enabled` and `needs_review` are never touched on an existing row, a
`risk_overridden` risk is kept, and so are capabilities whose `capability_source` is `admin`. A
spec that declares no capabilities (MCP, OpenAPI) never overwrites what a row already has, and a
new row for one starts `needs_review` until the capability tagger (Phase 6 C1) exists.
"""

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.security.credential_codec import decrypt_secrets
from relay_core.security.crypto import LocalKMS

# Gemini function names: start with a letter or underscore, then letters, digits, `_ . : -`. The
# installed google-genai SDK documents a 128-character cap and older docs say 64; 64 fits both.
_LLM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
_LLM_NAME_MAX = 64
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9_.:-]")


class SyncReport(BaseModel):
    added: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    error: str | None = None


def installation_context(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    installation: ConnectorInstallation,
    secrets: dict[str, str],
) -> ExecutionContext:
    # There's no real run/conversation outside an agent run (install, health check, discovery),
    # so the installation's own id fills those two required fields; no connector reads them here.
    return ExecutionContext(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=installation.id,
        conversation_id=installation.id,
        installation_id=str(installation.id),
        config=installation.config,
        secrets=secrets,
    )


def llm_name(slug: str, tool_name: str) -> str:
    """`{slug}__{tool_name}` (section 6.6) when that is already a valid function name. Otherwise
    the name is sanitized, truncated, and given a hash of the original, so two tools whose names
    sanitize or truncate to the same text still get distinct names."""
    raw = f"{slug}__{tool_name}"
    if _LLM_NAME.fullmatch(raw):
        return raw
    name = _NOT_ALLOWED.sub("_", raw)
    if not re.match(r"[A-Za-z_]", name):
        name = f"_{name}"
    suffix = "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return name[: _LLM_NAME_MAX - len(suffix)] + suffix


def schema_hash(description: str, input_schema: dict[str, Any]) -> str:
    # The description is hashed too: tool poisoning usually rides on the description.
    canonical = json.dumps(
        {"description": description, "input_schema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def sync_installation(
    session: AsyncSession, kms: LocalKMS, installation: ConnectorInstallation
) -> SyncReport:
    """Never raises for a discovery failure: the installation is marked `down` with the reason,
    its existing rows stay (a server blip must not wipe the tool list), and the report carries
    the error. Callers that loop over installations can therefore just keep going."""
    workspace_id = installation.workspace_id
    try:
        secrets: dict[str, str] = {}
        credential = await ConnectorCredentialRepository(session).get(workspace_id, installation.id)
        if credential is not None:
            secrets = decrypt_secrets(kms, credential)
        ctx = installation_context(workspace_id, installation.installed_by, installation, secrets)
        specs = await CONNECTOR_TYPES[installation.connector_key]().list_tools(ctx)
    except Exception as exc:  # noqa: BLE001 - recorded on the installation, not raised
        message = f"Tool discovery failed: {exc!r}"
        await ConnectorInstallationRepository(session).set_health(
            workspace_id, installation.id, health="down", message=message
        )
        return SyncReport(error=message)

    tools = ToolDefinitionRepository(session)
    rows = await tools.list_for_installation(workspace_id, installation.id)
    existing = {row.name: row for row in rows}
    report = SyncReport()
    # Keyed by name: a server that lists one name twice must not trip the unique constraint.
    for spec in {s.name: s for s in specs}.values():
        name = llm_name(installation.slug, spec.name)
        hash_ = schema_hash(spec.description, spec.input_schema)
        row = existing.pop(spec.name, None)
        if row is None:
            await tools.add(
                ToolDefinition(
                    workspace_id=workspace_id,
                    installation_id=installation.id,
                    name=spec.name,
                    llm_name=name,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    schema_hash=hash_,
                    risk=spec.risk.value,
                    capabilities=list(spec.capabilities),
                    capability_source="declared",
                    idempotent=spec.idempotent,
                    timeout_s=spec.timeout_s,
                    needs_review=not spec.capabilities,
                )
            )
            report.added.append(spec.name)
            if not spec.capabilities:
                report.needs_review.append(spec.name)
            continue

        desired: dict[str, Any] = {
            "llm_name": name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "schema_hash": hash_,
            "idempotent": spec.idempotent,
        }
        if not row.risk_overridden:
            desired["risk"] = spec.risk.value
        if spec.capabilities and row.capability_source != "admin":
            desired["capabilities"] = list(spec.capabilities)
            desired["capability_source"] = "declared"
        changed = {k: v for k, v in desired.items() if getattr(row, k) != v}
        # `timeout_s` is a REAL column, so a value like 0.1 reads back float32-rounded.
        if not math.isclose(row.timeout_s, spec.timeout_s, rel_tol=1e-6):
            changed["timeout_s"] = spec.timeout_s
        for key, value in changed.items():
            setattr(row, key, value)
        if changed:
            report.updated.append(spec.name)

    for row in existing.values():
        await tools.delete(workspace_id, row.id)
        report.removed.append(row.name)

    installation.last_synced_at = datetime.now(UTC)
    await session.flush()
    return report
