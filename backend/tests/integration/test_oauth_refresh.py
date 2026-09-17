"""OAuth token refresh, lazy and swept (Phase 7 A4, docs/system-design.md section 18.3 step 6).

The refresh goes to the mock service's `/oauth/token`, which issues a numbered access token each
time, so "did this refresh?" is a string comparison rather than a mock assertion.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.connectors.oauth import SWEEP_MARGIN_S, refresh_if_expiring
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.security.credential_codec import decrypt_secrets, encrypt_secrets
from relay_core.security.crypto import build_kms
from relay_core.tools.sync import installation_secrets

pytestmark = pytest.mark.asyncio


async def _connected_gmail(
    session: AsyncSession,
    settings: Settings,
    *,
    token_url: str,
    expires_in: timedelta,
    refresh_token: str = "mock-refresh-1",
) -> ConnectorInstallation:
    label = f"refresh-{uuid.uuid4().hex[:8]}"
    user = await UserRepository(session).create(
        email=f"{label}@example.com", full_name="Dev", password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name="Refresh", slug=label, created_by=user.id
    )
    installations = ConnectorInstallationRepository(session)
    installation = await installations.create(
        workspace_id=workspace.id,
        connector_key="gmail",
        name="Mailbox",
        slug="mailbox",
        config={"base_url": "http://mock-services:8100"},
        priority=100,
        installed_by=user.id,
    )
    await installations.set_health(
        workspace.id, installation.id, health="healthy", message="ok", status="active"
    )
    await ConnectorCredentialRepository(session).put(
        workspace_id=workspace.id,
        installation_id=installation.id,
        encrypted=encrypt_secrets(
            build_kms(settings),
            {
                "access_token": "stale-token",
                "refresh_token": refresh_token,
                "token_uri": token_url,
            },
        ),
        oauth_expires_at=datetime.now(UTC) + expires_in,
    )
    return installation


async def _stored(
    session: AsyncSession, settings: Settings, installation: ConnectorInstallation
) -> tuple[dict[str, str], datetime | None]:
    credential = await ConnectorCredentialRepository(session).get(
        installation.workspace_id, installation.id
    )
    assert credential is not None
    return decrypt_secrets(build_kms(settings), credential), credential.oauth_expires_at


async def test_a_call_that_finds_a_nearly_expired_token_refreshes_it_first(
    db_session: AsyncSession, test_settings: Settings, mock_services_url: str
) -> None:
    installation = await _connected_gmail(
        db_session,
        test_settings,
        token_url=f"{mock_services_url}/oauth/token",
        expires_in=timedelta(seconds=30),
    )

    secrets = await installation_secrets(
        db_session, build_kms(test_settings), installation, test_settings
    )

    assert secrets["access_token"].startswith("mock-access-")
    stored, expires_at = await _stored(db_session, test_settings, installation)
    assert stored["access_token"] == secrets["access_token"]
    assert expires_at is not None and expires_at > datetime.now(UTC) + timedelta(minutes=30)


async def test_a_token_with_an_hour_left_is_left_alone(
    db_session: AsyncSession, test_settings: Settings, mock_services_url: str
) -> None:
    installation = await _connected_gmail(
        db_session,
        test_settings,
        token_url=f"{mock_services_url}/oauth/token",
        expires_in=timedelta(hours=1),
    )

    secrets = await installation_secrets(
        db_session, build_kms(test_settings), installation, test_settings
    )

    assert secrets["access_token"] == "stale-token"


async def test_the_sweeps_wider_margin_is_the_only_difference_from_the_call_path(
    db_session: AsyncSession, test_settings: Settings, mock_services_url: str
) -> None:
    """Ten minutes out: too far for a tool call to bother, close enough for the beat sweep."""
    kms = build_kms(test_settings)
    installation = await _connected_gmail(
        db_session,
        test_settings,
        token_url=f"{mock_services_url}/oauth/token",
        expires_in=timedelta(minutes=10),
    )

    lazy = await installation_secrets(db_session, kms, installation, test_settings)
    assert lazy["access_token"] == "stale-token"

    credential = await ConnectorCredentialRepository(db_session).get(
        installation.workspace_id, installation.id
    )
    assert credential is not None
    swept = await refresh_if_expiring(
        db_session,
        kms,
        installation,
        credential,
        decrypt_secrets(kms, credential),
        test_settings,
        margin_s=SWEEP_MARGIN_S,
    )

    assert swept["access_token"].startswith("mock-access-")
    assert swept["refresh_token"] == "mock-refresh-1"  # carried forward, not dropped


async def test_a_revoked_grant_marks_the_installation_degraded(
    db_session: AsyncSession, test_settings: Settings, mock_services_url: str
) -> None:
    installation = await _connected_gmail(
        db_session,
        test_settings,
        token_url=f"{mock_services_url}/oauth/token",
        expires_in=timedelta(seconds=30),
        refresh_token="revoked",
    )

    secrets = await installation_secrets(
        db_session, build_kms(test_settings), installation, test_settings
    )

    assert secrets["access_token"] == "stale-token"
    refreshed = await ConnectorInstallationRepository(db_session).get(
        installation.workspace_id, installation.id
    )
    assert refreshed is not None and refreshed.health == "degraded"
    assert "reconnect" in (refreshed.health_message or "").lower()


async def test_an_unreachable_token_endpoint_does_not_mark_anything_degraded(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A blip must not cost an installation its health; the next call or sweep tries again."""
    installation = await _connected_gmail(
        db_session,
        test_settings,
        # Port 1 with nothing listening: a connection error, not an OAuth error.
        token_url="http://127.0.0.1:1/oauth/token",
        expires_in=timedelta(seconds=30),
    )

    secrets = await installation_secrets(
        db_session, build_kms(test_settings), installation, test_settings
    )

    assert secrets["access_token"] == "stale-token"
    unchanged = await ConnectorInstallationRepository(db_session).get(
        installation.workspace_id, installation.id
    )
    assert unchanged is not None and unchanged.health == "healthy"
