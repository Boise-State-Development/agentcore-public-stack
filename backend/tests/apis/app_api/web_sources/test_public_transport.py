"""The crawler's connection layer: resolve once, refuse non-public, connect to what was checked.

No real sockets: the resolver seam (`url_utils._getaddrinfo`) is replaced,
and the inner backend's `connect_tcp` is a spy that records the address it
was asked for and then fails, so each test sees exactly what would have
been dialled.
"""

from __future__ import annotations

from typing import List

import httpcore
import httpx
import pytest

from apis.app_api.web_sources import crawler, url_utils
from apis.app_api.web_sources.public_transport import PublicOnlyTransport
from apis.app_api.web_sources.url_utils import BlockedAddressError

_PUBLIC = "93.184.215.14"


@pytest.fixture
def dialled(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    connects: List[str] = []

    async def spy_connect(self, host: str, port: int, **kwargs):
        connects.append(host)
        raise httpcore.ConnectError("spy: no network in tests")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy_connect)
    return connects


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *answers: List[str]) -> List[str]:
    """Each lookup returns the next answer (a list of addresses)."""
    lookups: List[str] = []
    queue = list(answers)

    async def fake_getaddrinfo(host: str, port: int):
        lookups.append(host)
        addresses = queue.pop(0) if len(queue) > 1 else queue[0]
        return [(2 if ":" not in a else 30, 1, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(url_utils, "_getaddrinfo", fake_getaddrinfo)
    return lookups


async def _get(url: str) -> None:
    async with httpx.AsyncClient(transport=PublicOnlyTransport()) as client:
        await client.get(url)


@pytest.mark.parametrize("private", ["10.0.0.7", "169.254.169.254", "169.254.170.2", "127.0.0.1", "::1", "fd00::5"])
@pytest.mark.asyncio
async def test_a_name_resolving_privately_is_never_dialled(monkeypatch, dialled, private):
    _resolve_to(monkeypatch, [private])
    with pytest.raises(BlockedAddressError):
        await _get("https://looks-public.example.com/")
    assert dialled == []


@pytest.mark.asyncio
async def test_one_private_answer_among_public_ones_is_refused(monkeypatch, dialled):
    _resolve_to(monkeypatch, [_PUBLIC, "10.0.0.7"])
    with pytest.raises(BlockedAddressError):
        await _get("https://example.com/")
    assert dialled == []


@pytest.mark.asyncio
async def test_the_connection_is_pinned_to_the_checked_address(monkeypatch, dialled):
    # A rebinding resolver: public for the first lookup, private after.
    # The name is looked up once and the socket goes to that answer.
    lookups = _resolve_to(monkeypatch, [_PUBLIC], ["169.254.169.254"])
    with pytest.raises(httpx.ConnectError):
        await _get("https://rebind.example.com/")
    assert lookups == ["rebind.example.com"]
    assert dialled == [_PUBLIC]


@pytest.mark.asyncio
async def test_each_checked_address_is_tried_in_turn(monkeypatch, dialled):
    _resolve_to(monkeypatch, [_PUBLIC, "93.184.215.15"])
    with pytest.raises(httpx.ConnectError):
        await _get("http://example.com/")
    assert dialled == [_PUBLIC, "93.184.215.15"]


@pytest.mark.asyncio
async def test_a_private_ip_literal_is_refused_without_dns(monkeypatch, dialled):
    lookups = _resolve_to(monkeypatch, [_PUBLIC])
    with pytest.raises(BlockedAddressError):
        await _get("http://169.254.169.254/latest/meta-data/")
    assert lookups == []
    assert dialled == []


def test_the_crawlers_default_client_uses_it():
    client = crawler._default_client()
    assert isinstance(client._transport, PublicOnlyTransport)
