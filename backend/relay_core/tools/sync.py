"""Tool discovery sync (docs/system-design.md sections 6.4 "Discovery" and 17.3): turns one
installation's `connector.list_tools(ctx)` into its `tool_definitions` rows. Every installed
connector goes through here, built-ins included, so review state, admin overrides and retrieval
embeddings live in one place whatever a tool's source.

Upsert rules for an existing row:

- `is_enabled` and `needs_review` are kept, except that an **MCP** tool whose description or
  schema changed upstream is disabled and sent back to review (section 18.1, "Malicious MCP
  server": a tool approved as harmless must not quietly become something else). Built-in specs
  are trusted code and OpenAPI operations are frozen at install, so theirs update silently.
- A `risk_overridden` risk is kept, and so is a tagger-assigned risk: the tagger only ever raises
  a connector's default, so resetting it to the default would lower it.
- Capabilities whose `capability_source` is `admin` are kept. A spec that declares capabilities
  (built-ins) sets them; one that declares none (MCP, OpenAPI) goes to the capability tagger when
  the tool is new or changed, and is left in review if the tagger can't place it.

Tagging and embedding need an `LLMGateway`. Without one they're skipped, and the rows are still
correct, just untagged and unranked.
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

from relay_core.capabilities.tagger import TagResult, tag_tools
from relay_core.config import Settings, get_settings
from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.security.credential_codec import decrypt_secrets
from relay_core.security.crypto import LocalKMS

# Gemini function names: start with a letter or underscore, then letters, digits, `_ . : -`. The
# installed google-genai SDK documents a 128-character cap and older docs say 64; 64 fits both.
_LLM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
_LLM_NAME_MAX = 64
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9_.:-]")
_REVIEWED_ON_CHANGE = frozenset({"mcp"})


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


async def installation_secrets(
    session: AsyncSession, kms: LocalKMS, installation: ConnectorInstallation
) -> dict[str, str]:
    credential = await ConnectorCredentialRepository(session).get(
        installation.workspace_id, installation.id
    )
    return decrypt_secrets(kms, credential) if credential is not None else {}


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
    session: AsyncSession,
    kms: LocalKMS,
    installation: ConnectorInstallation,
    *,
    gateway: LLMGateway | None = None,
    settings: Settings | None = None,
) -> SyncReport:
    """Never raises for a discovery failure: the installation is marked `down` with the reason,
    its existing rows stay (a server blip must not wipe the tool list), and the report carries
    the error. Callers that loop over installations can therefore just keep going."""
    workspace_id = installation.workspace_id
    try:
        secrets = await installation_secrets(session, kms, installation)
        ctx = installation_context(workspace_id, installation.installed_by, installation, secrets)
        specs = await CONNECTOR_TYPES[installation.connector_key]().list_tools(ctx)
    except Exception as exc:  # noqa: BLE001 - recorded on the installation, not raised
        message = f"Tool discovery failed: {exc!r}"
        await ConnectorInstallationRepository(session).set_health(
            workspace_id, installation.id, health="down", message=message
        )
        return SyncReport(error=message)

    llm = (gateway, settings or get_settings()) if gateway is not None else None
    tools = ToolDefinitionRepository(session)
    rows = await tools.list_for_installation(workspace_id, installation.id)
    existing = {row.name: row for row in rows}
    # Keyed by name: a server that lists one name twice must not trip the unique constraint.
    unique_specs = list({s.name: s for s in specs}.values())
    hashes = {s.name: schema_hash(s.description, s.input_schema) for s in unique_specs}

    changed_upstream = {
        s.name
        for s in unique_specs
        if s.name not in existing or existing[s.name].schema_hash != hashes[s.name]
    }

    tags: dict[str, TagResult] = {}
    to_tag = [
        s
        for s in unique_specs
        if not s.capabilities
        and s.name in changed_upstream
        and (s.name not in existing or existing[s.name].capability_source != "admin")
    ]
    if llm is not None and to_tag:
        tags = await tag_tools(llm[0], llm[1], workspace_id, to_tag)

    report = SyncReport()
    to_embed: list[ToolDefinition] = []
    for spec in unique_specs:
        tag = tags.get(spec.name)
        row = existing.pop(spec.name, None)
        if row is None:
            row = await tools.add(
                ToolDefinition(
                    workspace_id=workspace_id,
                    installation_id=installation.id,
                    name=spec.name,
                    llm_name=llm_name(installation.slug, spec.name),
                    description=spec.description,
                    input_schema=spec.input_schema,
                    schema_hash=hashes[spec.name],
                    risk=(tag.risk if tag else spec.risk).value,
                    capabilities=tag.capabilities if tag else list(spec.capabilities),
                    capability_source="tagged" if tag else "declared",
                    tag_confidence=tag.confidence if tag else None,
                    idempotent=spec.idempotent,
                    timeout_s=spec.timeout_s,
                    needs_review=tag.needs_review if tag else not spec.capabilities,
                )
            )
            report.added.append(spec.name)
            if row.needs_review:
                report.needs_review.append(spec.name)
            to_embed.append(row)
            continue

        rug_pull = (
            installation.connector_key in _REVIEWED_ON_CHANGE and spec.name in changed_upstream
        )
        desired: dict[str, Any] = {
            "llm_name": llm_name(installation.slug, spec.name),
            "description": spec.description,
            "input_schema": spec.input_schema,
            "schema_hash": hashes[spec.name],
            "idempotent": spec.idempotent,
        }
        if spec.capabilities and row.capability_source != "admin":
            desired["capabilities"] = list(spec.capabilities)
            desired["capability_source"] = "declared"
        if tag is not None:
            desired["capabilities"] = tag.capabilities
            desired["capability_source"] = "tagged"
            desired["tag_confidence"] = tag.confidence
            desired["needs_review"] = tag.needs_review
        if not row.risk_overridden:
            if tag is not None:
                desired["risk"] = tag.risk.value
            elif row.capability_source != "tagged":
                desired["risk"] = spec.risk.value
        if rug_pull:
            desired["needs_review"] = True
            desired["is_enabled"] = False
        changed = {k: v for k, v in desired.items() if getattr(row, k) != v}
        # `timeout_s` is a REAL column, so a value like 0.1 reads back float32-rounded.
        if not math.isclose(row.timeout_s, spec.timeout_s, rel_tol=1e-6):
            changed["timeout_s"] = spec.timeout_s
        for key, value in changed.items():
            setattr(row, key, value)
        if changed:
            report.updated.append(spec.name)
            if row.needs_review and "needs_review" in changed:
                report.needs_review.append(spec.name)
        if llm is not None and (
            spec.name in changed_upstream
            or row.embedding is None
            or row.embedding_model != llm[1].embedding_model
        ):
            to_embed.append(row)

    for row in existing.values():
        await tools.delete(workspace_id, row.id)
        report.removed.append(row.name)

    if llm is not None and to_embed:
        await _embed(llm[0], llm[1], to_embed)

    installation.last_synced_at = datetime.now(UTC)
    await session.flush()
    return report


async def _embed(gateway: LLMGateway, settings: Settings, rows: list[ToolDefinition]) -> None:
    """Vectors for tool retrieval (section 6.7 step 4). A failure leaves them null: the tool still
    binds, it just ranks last until a later sync embeds it."""
    texts = [
        f"{row.name}\n{row.description}\nCapabilities: {', '.join(row.capabilities)}"
        for row in rows
    ]
    try:
        vectors = await gateway.embed(texts, task="RETRIEVAL_DOCUMENT", settings=settings)
    except Exception:  # noqa: BLE001 - embeddings are an optimisation, never a sync failure
        return
    for row, vector in zip(rows, vectors, strict=False):
        row.embedding = vector
        row.embedding_model = settings.embedding_model
