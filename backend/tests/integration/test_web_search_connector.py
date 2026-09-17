"""The `web_search` connector (Phase 6 B6) against the mock service."""

import uuid

import pytest

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.web_search import WebSearchConnector

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("ssrf_allows_localhost")]


def _ctx(base_url: str) -> ExecutionContext:
    run_id = uuid.uuid4()
    return ExecutionContext(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=run_id,
        conversation_id=run_id,
        installation_id="web",
        config={"base_url": base_url},
    )


async def test_search_returns_the_fixture_results(mock_services_url: str) -> None:
    result = await WebSearchConnector().call_tool(
        _ctx(mock_services_url), "search_web", {"query": "Acme Robotics funding", "max_results": 2}
    )

    assert result.ok, result.error
    assert result.content[0]["title"] == "Acme Robotics raises Series C"
    assert set(result.content[0]) == {"title", "url", "snippet", "published_date"}


async def test_fetch_keeps_the_text_and_drops_scripts_and_styles(mock_services_url: str) -> None:
    result = await WebSearchConnector().call_tool(
        _ctx(mock_services_url),
        "fetch_url",
        {"url": f"{mock_services_url}/pages/acme-robotics-funding"},
    )

    assert result.ok, result.error
    text = result.content["text"]
    assert "Acme Robotics announced a $120M Series C" in text
    assert "do-not-return-this" not in text and "font-family" not in text
    assert "Enable JavaScript" not in text
    assert "  " not in text and "\n" not in text


async def test_fetching_the_metadata_address_is_refused(mock_services_url: str) -> None:
    result = await WebSearchConnector().call_tool(
        _ctx(mock_services_url), "fetch_url", {"url": "http://169.254.169.254/"}
    )

    assert result.ok is False
    assert "non-public address" in (result.error or "")
