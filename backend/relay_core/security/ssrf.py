"""SSRF guard (docs/system-design.md section 18.5) for every URL a workspace admin or the model
supplies: MCP server URLs, OpenAPI spec and base URLs, `fetch_url`.

Two entry points. `check_url` validates without connecting, so an install route can reject a bad
URL with a readable 400. `guarded_client` is what actually makes requests: its transport resolves
and checks the host on *every* request, each redirect hop included, then connects to the address
it checked. Checking once and letting the socket resolve again would leave a DNS-rebinding window:
a public answer for the check, `169.254.169.254` for the connect.

`SSRFBlocked` subclasses `httpx.RequestError`, so a connector's existing `except httpx.HTTPError`
turns a blocked URL into an ordinary tool error without a handler of its own.

`guarded_mcp_http_client` is the same guard for the `mcp` SDK, which is built on `httpx2`: a fork
with the same API but its own classes, so an `httpx` client can't be handed to it.

`settings.ssrf_allowed_hosts` exempts exact hostnames from the address check, never from the
scheme check: the dev stack's own services (`mock-services`, test servers on `127.0.0.1`) live on
private addresses by definition.
"""

import asyncio
import ipaddress
import socket
from typing import Any

import httpx
import httpx2

from relay_core.config import get_settings

MAX_RESPONSE_BYTES = 2_000_000
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_REDIRECTS = 3


class SSRFBlocked(httpx.RequestError):
    pass


async def check_url(url: str) -> None:
    parsed = httpx.URL(url)
    await _pinned_address(parsed.scheme, parsed.host)


def guarded_client(**kwargs: Any) -> httpx.AsyncClient:
    defaults: dict[str, Any] = {
        "follow_redirects": True,
        "max_redirects": _MAX_REDIRECTS,
        "timeout": _TIMEOUT,
    }
    return httpx.AsyncClient(transport=_GuardedTransport(), **{**defaults, **kwargs})


def guarded_mcp_http_client(**kwargs: Any) -> httpx2.AsyncClient:
    """No redirect or timeout defaults: the `mcp` SDK follows same-origin redirects itself and
    holds streams open longer than a plain request would."""
    return httpx2.AsyncClient(transport=_GuardedMCPTransport(), **kwargs)


async def read_capped(response: httpx.Response, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """Reads a *streamed* response (`client.stream(...)`) and gives up once it passes
    `max_bytes`, so an endless body can't exhaust worker memory."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SSRFBlocked(f"Response exceeds {max_bytes} bytes")
    return bytes(body)


class _GuardedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await super().handle_async_request(await _pin(request, httpx.Request))


class _GuardedMCPTransport(httpx2.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await super().handle_async_request(await _pin(request, httpx2.Request))


async def _pin(request: Any, request_cls: Any) -> Any:
    """`request` re-aimed at the address that was checked. Duck-typed over `httpx` and `httpx2`,
    whose `Request`s share this API."""
    address = await _pinned_address(request.url.scheme, request.url.host)
    if address is None:
        return request
    # A copy, not an in-place edit: the client keeps `request` for resolving redirects and as
    # `response.request`, and both must still name the hostname rather than the address.
    return request_cls(
        request.method,
        request.url.copy_with(host=address),
        headers=request.headers,  # still carries `Host: <hostname>`
        stream=request.stream,
        extensions={**request.extensions, "sni_hostname": request.url.host},
    )


async def _pinned_address(scheme: str, host: str) -> str | None:
    """The checked address to connect to, or `None` for an allow-listed host."""
    settings = get_settings()
    allowed_schemes = {"https", "http"} if settings.env == "dev" else {"https"}
    if scheme not in allowed_schemes:
        raise SSRFBlocked(f"URL scheme {scheme!r} is not allowed")
    if not host:
        raise SSRFBlocked("URL has no host")
    if host in {h.lower() for h in settings.ssrf_allowed_hosts}:
        return None

    addresses = await _resolve(host)
    if not addresses:
        raise SSRFBlocked(f"Cannot resolve host {host!r}")
    for address in addresses:
        if _is_blocked(address):
            raise SSRFBlocked(f"Host {host!r} resolves to a non-public address ({address})")
    return addresses[0]


async def _resolve(host: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_blocked(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # `is_global` is false for private, loopback, link-local, reserved, unspecified and
    # 100.64.0.0/10, but true for multicast, which has to be ruled out separately.
    return not ip.is_global or ip.is_multicast
