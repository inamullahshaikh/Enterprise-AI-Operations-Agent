from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central app configuration, loaded from environment variables / .env.

    See docs/system-design.md §25 for the full variable reference.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "staging", "prod"] = "dev"
    app_base_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"

    # Database / cache / storage
    database_url: str
    langgraph_db_url: str
    redis_url: str

    # Demo company database (the "customer" the agent queries) — only used by
    # `relay_worker.tasks.maintenance.seed_demo` to install a postgres connector pointing at
    # it; nothing in the request path reads this.
    demo_db_url: str = ""

    # Object storage — Cloudflare R2 (S3-compatible API), used in every environment.
    # boto3 clients must be created with region_name="auto" and addressing_style="path".
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "relay"
    r2_endpoint_url: str = ""

    # Auth
    jwt_private_key_path: str = "/secrets/jwt_ed25519.pem"
    jwt_public_key_path: str = "/secrets/jwt_ed25519.pub"
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 14

    # Google Sign-In (user login, alongside email/password). Reused with broader
    # scopes by the Gmail/Calendar connector at install time (docs/adr/0007).
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    # Overridable so tests and the dev stack can point the token exchange at the mock service
    # instead of Google (docs/phase-7-tickets.md A1).
    google_token_url: str = "https://oauth2.googleapis.com/token"

    # Encryption
    kms_provider: Literal["local", "aws"] = "local"
    local_master_key: str | None = None
    aws_kms_key_id: str | None = None

    # Gemini
    gemini_api_key: str = ""
    model_planner: str = "gemini-3.8-flash"
    model_planner_thinking: str = "high"
    model_executor: str = "gemini-3.8-flash"
    model_executor_thinking: str = "low"
    model_validator: str = "gemini-3.8-flash"
    model_validator_thinking: str = "medium"
    model_light: str = "gemini-3.5-flash-lite"
    model_light_thinking: str = "minimal"
    model_fallback_executor: str = "gemini-3.7-flash"
    model_escalation: str | None = None
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    gemini_rpm_limit: int = 60

    # Connectors
    sandbox_url: str = "http://sandbox:8080"
    sandbox_timeout_s: float = 30.0
    use_mock_connectors: bool = True
    # Where `mocks/main.py` answers for gmail/google_calendar (section 21.2). Read by
    # `seed_demo` and the eval harness when they install those connectors; an installation
    # stores its own `base_url`, so nothing in the request path reads this.
    mock_services_url: str = "http://mock-services:8100"
    # The sample MCP server (mcp_examples/ticketing). Only the eval harness installs it; the demo
    # plugs it in live through the API instead (section 27.2 step 7).
    mcp_ticketing_url: str = "http://mcp-ticketing:8200/mcp"
    # Exact hostnames `relay_core.security.ssrf` lets through to private addresses — the dev
    # stack's own services. Set as a JSON list, e.g. ["mock-services","mcp-ticketing"].
    ssrf_allowed_hosts: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
