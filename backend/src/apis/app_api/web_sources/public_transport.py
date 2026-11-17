"""An httpx transport that connects only to public addresses.

The crawler fetches URLs that other people's pages name, so every
connection it opens is a possible SSRF: a hostname that resolves to
169.254.169.254 (the EC2 metadata service), 169.254.170.2 (ECS task
credentials) or an RFC 1918 host inside the VPC.

Checking the hostname before the request is not enough on its own: the
connection resolves it again, and a rebinding resolver can answer with a
public address the first time and a private one the second. So the check
lives in the network backend, at the one place a socket is opened. It
resolves the name once, refuses it if any address is non-public, and
connects to an address it checked. TLS still verifies the certificate
against the hostname, and the connection pool still keys on it, because
only the TCP connect sees the address.
"""

import logging
from typing import Iterable, Optional

import httpcore
import httpx

from apis.app_api.web_sources.url_utils import resolve_public_addresses

logger = logging.getLogger(__name__)

# httpx's own defaults (`httpx.AsyncHTTPTransport()`), restated because the
# pool is built here rather than by httpx.
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE_CONNECTIONS = 20
_KEEPALIVE_EXPIRY_SECONDS = 5.0


class PublicOnlyNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves, checks and connects in one step; see the module docstring."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[httpcore.SOCKET_OPTION]] = None,
    ) -> httpcore.AsyncNetworkStream:
        # Raises BlockedAddressError before any socket is opened.
        addresses = await resolve_public_addresses(host, port)
        last_error: Optional[Exception] = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                logger.debug("Connect to %s (%s) failed: %s", host, address, e)
                last_error = e
        raise last_error or httpcore.ConnectError(f"No address to connect to for {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Optional[Iterable[httpcore.SOCKET_OPTION]] = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not reachable from a crawl")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """`httpx.AsyncHTTPTransport` whose every connection is `PublicOnlyNetworkBackend`'s.

    Passing a transport to `httpx.AsyncClient` also turns off proxies from
    the environment, which matters: a proxy would make the connection on
    our behalf, out of this backend's sight.
    """

    def __init__(self) -> None:
        super().__init__()
        # httpx 0.28 has no public way to hand its pool a network backend,
        # so the pool it built is replaced with an equivalent one that has
        # ours. `test_public_transport.py` fails if this stops taking effect.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
            network_backend=PublicOnlyNetworkBackend(),
        )
