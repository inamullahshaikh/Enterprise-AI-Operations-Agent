"""The SSRF guard (docs/system-design.md section 18.5). DNS is patched so every case is
deterministic offline; upstream HTTP is patched at `httpx.AsyncHTTPTransport`, underneath the
guard's own transport, so what's asserted is exactly what would have gone out on the wire.
"""

from collections.abc import Callable

import httpx
import pytest

from relay_core.config import Settings
from relay_core.security import ssrf

_PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch, test_settings: Settings) -> Settings:
    patched = test_settings.model_copy(
        update={"env": "prod", "ssrf_allowed_hosts": ["Mock-Services"]}
    )
    monkeypatch.setattr(ssrf, "get_settings", lambda: patched)
    return patched


def _dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> None:
    async def resolve(host: str) -> list[str]:
        return table.get(host, [host])

    monkeypatch.setattr(ssrf, "_resolve", resolve)


def _upstream(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    async def send(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return sent


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "fc00::1",
        "fe80::1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
async def test_blocks_non_public_addresses(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    _dns(monkeypatch, {"target.example": [address]})
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.check_url("https://target.example/")


async def test_real_resolver_blocks_a_literal_loopback() -> None:
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.check_url("https://127.0.0.1/")


async def test_blocks_when_any_resolved_address_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"mixed.example": [_PUBLIC, "10.0.0.5"]})
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.check_url("https://mixed.example/")


async def test_allows_a_public_https_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC]})
    await ssrf.check_url("https://public.example/")


async def test_http_is_blocked_outside_dev_and_allowed_in_dev(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC]})
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.check_url("http://public.example/")
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.check_url("file:///etc/passwd")

    monkeypatch.setattr(settings, "env", "dev")
    await ssrf.check_url("http://public.example/")


async def test_allow_listed_host_skips_the_address_check(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _dns(monkeypatch, {"mock-services": ["172.18.0.4"]})
    await ssrf.check_url("https://mock-services:8100/")

    monkeypatch.setattr(settings, "env", "dev")
    await ssrf.check_url("http://mock-services:8100/")


async def test_connects_to_the_checked_address_and_keeps_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC]})
    sent = _upstream(monkeypatch, lambda _: httpx.Response(200, text="ok"))

    async with ssrf.guarded_client() as client:
        resp = await client.get("https://public.example/path?q=1")

    assert sent[0].url.host == _PUBLIC
    assert sent[0].url.raw_path == b"/path?q=1"
    assert sent[0].headers["host"] == "public.example"
    assert sent[0].extensions["sni_hostname"] == "public.example"
    assert resp.request.url.host == "public.example"


async def test_rechecks_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS rebinding: the second lookup answers with a private address."""
    answers = iter([[_PUBLIC], ["127.0.0.1"]])

    async def resolve(host: str) -> list[str]:
        return next(answers)

    monkeypatch.setattr(ssrf, "_resolve", resolve)
    _upstream(monkeypatch, lambda _: httpx.Response(200))

    async with ssrf.guarded_client() as client:
        await client.get("https://rebind.example/")
        with pytest.raises(ssrf.SSRFBlocked):
            await client.get("https://rebind.example/")


async def test_redirect_to_a_private_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC], "internal.example": ["10.0.0.1"]})
    sent = _upstream(
        monkeypatch,
        lambda _: httpx.Response(302, headers={"Location": "https://internal.example/admin"}),
    )

    async with ssrf.guarded_client() as client:
        with pytest.raises(ssrf.SSRFBlocked):
            await client.get("https://public.example/")
    assert len(sent) == 1


async def test_a_fourth_redirect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC]})
    sent = _upstream(monkeypatch, lambda _: httpx.Response(302, headers={"Location": "/again"}))

    async with ssrf.guarded_client() as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("https://public.example/")
    assert len(sent) == 4


async def test_body_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, {"public.example": [_PUBLIC]})
    _upstream(monkeypatch, lambda _: httpx.Response(200, content=b"x" * 11))

    async with ssrf.guarded_client() as client:
        async with client.stream("GET", "https://public.example/") as resp:
            with pytest.raises(ssrf.SSRFBlocked):
                await ssrf.read_capped(resp, max_bytes=10)
        async with client.stream("GET", "https://public.example/") as resp:
            assert await ssrf.read_capped(resp, max_bytes=11) == b"x" * 11
