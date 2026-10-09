"""Tests for ``apis.shared.security.url_validator`` integration in
``agents.local_tools.url_fetcher``.

Locks in the invariant that ``fetch_url_content`` runs every URL it
receives through the SSRF validator before any network I/O, and refuses
to follow redirects that would land on disallowed targets. The
authoritative implementation of the validator itself lives in
``apis.shared.security.url_validator`` and is exhaustively tested under
``tests/security/``; these tests are the integration-layer assertions
that the tool actually wires into it.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.local_tools.url_fetcher import fetch_url_content


def _underlying(tool_obj):
    inner = getattr(tool_obj, "_tool_func", None) or getattr(tool_obj, "func", None) or tool_obj
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    def _impl(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        results = []
        for ip in mapping[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 0, "", (ip, 0)))
        return results

    return _impl


# ---------------------------------------------------------------------------
# Pre-flight URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/",
        "http://127.0.0.1:8000/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
@pytest.mark.asyncio
async def test_disallowed_targets_short_circuit_before_network(url: str) -> None:
    impl = _underlying(fetch_url_content)
    with patch("httpx.AsyncClient") as client_cls:
        result = await impl(url=url)

    assert result["status"] == "error"
    # Network client must never have been constructed.
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_dns_resolving_to_private_ip_is_rejected() -> None:
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"sneaky.example.com": ["10.0.0.42"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake), patch("httpx.AsyncClient") as client_cls:
        result = await impl(url="https://sneaky.example.com/x")

    assert result["status"] == "error"
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_disallowed_scheme_rejected() -> None:
    impl = _underlying(fetch_url_content)
    with patch("httpx.AsyncClient") as client_cls:
        result = await impl(url="file:///etc/passwd")

    assert result["status"] == "error"
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_rejection_does_not_leak_resolved_address_in_message() -> None:
    impl = _underlying(fetch_url_content)
    result = await impl(url="http://169.254.169.254/")

    body = result["content"][0]["json"]
    assert "169.254" not in body.get("error", "")
    assert "metadata" not in body.get("error", "").lower()


# ---------------------------------------------------------------------------
# Public URLs still work
# ---------------------------------------------------------------------------


def _mock_response(response_payload, peer: str | None = None):
    """A streamed response; ``peer`` is the address the connection reached."""
    response = MagicMock()
    response.status_code = response_payload.get("status_code", 200)
    response.text = response_payload.get("text", "<html><title>Hi</title><body>ok</body></html>")
    response.headers = response_payload.get("headers", {"content-type": "text/html"})
    response.raise_for_status = MagicMock()
    response.aread = AsyncMock(return_value=b"")
    response.aclose = AsyncMock(return_value=None)
    if peer is None:
        response.extensions = {}
    else:
        stream = MagicMock()
        stream.get_extra_info = MagicMock(side_effect=lambda key: (peer, 443) if key == "server_addr" else None)
        response.extensions = {"network_stream": stream}
    return response


def _mock_client(*responses):
    """Build a context-manager AsyncMock that yields a client whose .send
    returns the given responses in order."""
    client = MagicMock()
    client.build_request = MagicMock(side_effect=lambda method, url, headers=None: (method, url))
    client.send = AsyncMock(side_effect=list(responses))

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, client


def _mock_async_client(response_payload, peer: str | None = None):
    return _mock_client(_mock_response(response_payload, peer))


@pytest.mark.asyncio
async def test_public_url_passes_validator_and_is_fetched() -> None:
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    cm, client = _mock_async_client({})

    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake), patch("httpx.AsyncClient", return_value=cm):
        result = await impl(url="https://example.com/")

    assert result["status"] == "success"
    client.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_does_not_follow_redirects_automatically() -> None:
    """The httpx client must be constructed with follow_redirects=False so
    a 302 to an internal address can be caught and re-validated."""
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    cm, _ = _mock_async_client({})

    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake), patch("httpx.AsyncClient", return_value=cm) as client_cls:
        await impl(url="https://example.com/")

    # The kwargs passed to AsyncClient must include follow_redirects=False.
    _, kwargs = client_cls.call_args
    assert kwargs.get("follow_redirects") is False


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_not_followed() -> None:
    """A 302 whose Location header points at a private address must not be
    followed; the tool either returns the 302 as-is or fails closed."""
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"redirect.example.com": ["93.184.216.34"]})

    response = _mock_response(
        {
            "status_code": 302,
            "text": "",
            "headers": {
                "content-type": "text/html",
                "location": "http://169.254.169.254/latest/meta-data/",
            },
        }
    )
    cm, client = _mock_client(response)

    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake), patch("httpx.AsyncClient", return_value=cm):
        result = await impl(url="https://redirect.example.com/")

    # The single request is the only call — no follow-up to the metadata IP.
    assert client.send.await_count == 1
    # Either: surface a redirect that was not followed, or fail closed.
    # Neither path calls out to the private target.
    if result["status"] == "success":
        body = result["content"][0]["json"]
        # If we surfaced the 302 itself, it must be the original URL's
        # response, not a follow-up fetch's body.
        assert body["status_code"] == 302


# ---------------------------------------------------------------------------
# Connected-peer check (DNS rebinding between validation and connect)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["169.254.169.254", "10.0.0.7", "127.0.0.1", "::ffff:169.254.169.254"])
@pytest.mark.asyncio
async def test_response_from_forbidden_peer_is_discarded(peer: str) -> None:
    """A host that validated as public but connected somewhere forbidden
    (a rebinding DNS answer on the client's own lookup) must not return
    its body to the model."""
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"rebind.example.com": ["93.184.216.34"]})
    response = _mock_response({"text": "AccessKeyId SecretAccessKey Token"}, peer=peer)
    cm, _ = _mock_client(response)

    with (
        patch("apis.shared.security.url_validator.socket.getaddrinfo", fake),
        patch("httpx.AsyncClient", return_value=cm),
        patch("agents.local_tools.url_fetcher.urllib.request.getproxies", return_value={}),
    ):
        result = await impl(url="https://rebind.example.com/")

    assert result["status"] == "error"
    assert "SecretAccessKey" not in str(result)
    # Body is never read from the forbidden connection, which is closed.
    response.aread.assert_not_awaited()
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_response_from_public_peer_is_returned() -> None:
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    cm, _ = _mock_async_client({}, peer="93.184.216.34")

    with (
        patch("apis.shared.security.url_validator.socket.getaddrinfo", fake),
        patch("httpx.AsyncClient", return_value=cm),
        patch("agents.local_tools.url_fetcher.urllib.request.getproxies", return_value={}),
    ):
        result = await impl(url="https://example.com/")

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_peer_check_is_skipped_behind_a_forward_proxy() -> None:
    """Behind a configured proxy the peer is the proxy itself, which is
    commonly a private address; the proxy is then the egress boundary."""
    impl = _underlying(fetch_url_content)
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    cm, _ = _mock_async_client({}, peer="10.0.0.3")

    with (
        patch("apis.shared.security.url_validator.socket.getaddrinfo", fake),
        patch("httpx.AsyncClient", return_value=cm),
        patch("agents.local_tools.url_fetcher.urllib.request.getproxies", return_value={"https": "http://10.0.0.3:3128"}),
    ):
        result = await impl(url="https://example.com/")

    assert result["status"] == "success"
