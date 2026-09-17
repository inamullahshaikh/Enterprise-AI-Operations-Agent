"""The `web_search` connector (docs/system-design.md section 10.5): `web.search` and `web.fetch`.

`search_web` speaks Tavily's request and response shape, so pointing `config.base_url` at
`https://api.tavily.com` with a real key is the only change from the mock service (section 21.2).

`fetch_url` fetches whatever URL the model names, which makes it the most SSRF-exposed tool in
Relay: every request, redirect hops included, goes through the SSRF guard, and the body is capped
before it's read into memory. HTML comes back as plain text with scripts and styles dropped.
Everything it returns is someone else's content, and `execute_step` wraps it as untrusted.
"""

from html.parser import HTMLParser
from typing import Any

import httpx
from pydantic import BaseModel

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.security.ssrf import guarded_client, read_capped

_MAX_TEXT = 20_000


class _Config(BaseModel):
    base_url: str
    max_results: int = 5


class WebSearchConnector(Connector):
    key = "web_search"
    display_name = "Web search"
    auth_type = AuthType.API_KEY

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search_web",
                description="Search the public web. Returns titles, URLs and short snippets.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "recency_days": {
                            "type": "integer",
                            "description": "Only results published in the last N days.",
                        },
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                },
                risk=Risk.READ,
                capabilities=["web.search"],
            ),
            ToolSpec(
                name="fetch_url",
                description="Fetch a public web page and return its text.",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {"url": {"type": "string"}},
                },
                risk=Risk.READ,
                capabilities=["web.fetch"],
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        config = _Config.model_validate(ctx.config)
        try:
            if tool_name == "search_web":
                return await _search(config, ctx.secrets.get("api_key"), args)
            if tool_name == "fetch_url":
                return await _fetch(args["url"])
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Web request failed: {exc}")
        return ToolResult(ok=False, error=f"Unknown tool {tool_name!r}")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        try:
            config = _Config.model_validate(ctx.config)
            async with guarded_client() as http:
                await http.get(config.base_url)
        except (ValueError, httpx.HTTPError) as exc:
            return False, f"Cannot reach the search API: {exc}"
        return True, "Search API reachable"


async def _search(config: _Config, api_key: str | None, args: dict[str, Any]) -> ToolResult:
    body: dict[str, Any] = {
        "query": args["query"],
        "max_results": args.get("max_results", config.max_results),
    }
    if args.get("recency_days"):
        body["days"] = args["recency_days"]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with guarded_client() as http:
        resp = await http.post(f"{config.base_url.rstrip('/')}/search", json=body, headers=headers)
    resp.raise_for_status()
    return ToolResult(
        ok=True,
        content=[
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "published_date": r.get("published_date"),
            }
            for r in resp.json().get("results", [])
        ],
    )


async def _fetch(url: str) -> ToolResult:
    async with guarded_client() as http, http.stream("GET", url) as resp:
        raw = await read_capped(resp)
    if resp.status_code >= 400:
        return ToolResult(ok=False, error=f"HTTP {resp.status_code} fetching {url}")
    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    if content_type == "text/html":
        parser = _TextOnly()
        parser.feed(text)
        text = " ".join(" ".join(parser.parts).split())
    elif content_type != "text/plain" and "json" not in content_type:
        return ToolResult(ok=False, error=f"Unsupported content type {content_type!r}")
    return ToolResult(
        ok=True,
        content={"url": url, "text": text[:_MAX_TEXT]},
        truncated=len(text) > _MAX_TEXT,
    )


class _TextOnly(HTMLParser):
    _SKIPPED = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)
