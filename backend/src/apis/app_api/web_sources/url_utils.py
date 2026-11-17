"""URL normalization, crawl scope, and SSRF guards.

Kept in one module so the route validator and the crawler share the exact
same logic — divergence between "is this URL safe to start a crawl from?"
and "is this discovered link safe to follow?" is how SSRF holes get reborn.
"""

import asyncio
import ipaddress
import socket
from typing import Iterable, List, Optional, Tuple, Union
from urllib.parse import urlparse, urlunparse


class InvalidUrlError(ValueError):
    """The supplied string is not a URL we will fetch."""


class BlockedAddressError(InvalidUrlError):
    """The URL's host is, or resolves to, a non-public address.

    Raised before any request is made to it — by the crawler for each
    redirect hop, and by its connection layer for each new connection.
    """


IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def normalize_url(url: str) -> str:
    """Lower-case scheme + host, drop fragment, collapse default ports.

    Used as the dedupe key inside a crawl and as the `source_file_id` on
    each document — different casings of the same URL must map to the same
    record.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise InvalidUrlError("URL must include a scheme and host")
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(f"Unsupported URL scheme: {scheme}")
    netloc = parsed.netloc.lower()
    # Strip the default port for the scheme — http://x.com:80 == http://x.com
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def host_of(url: str) -> Optional[str]:
    """Return the lower-case host of a URL, or None if it can't be parsed."""
    try:
        parsed = urlparse(url)
        return parsed.hostname.lower() if parsed.hostname else None
    except (ValueError, AttributeError):
        return None


def _host_without_www(netloc: str) -> str:
    """`www.example.com` and `example.com` are one site for crawl scope.

    Most sites redirect one to the other and then link absolutely to the
    canonical form, so a crawl started at `example.com/research/` reads
    links to `www.example.com/research/…` from its very first page.
    """
    return netloc[4:] if netloc.startswith("www.") else netloc


def crawl_scope_prefix(root_url: str) -> str:
    """The path a crawl started at `root_url` is confined to.

    The root names a section of a site, and the crawl stays inside it:
    `/research/` and `/research` both confine it to `/research/`, a page
    such as `/research/index.html` to its directory, and the site's home
    page to the whole host. `/research-news/` is outside `/research/`.
    """
    path = urlparse(root_url).path or "/"
    if path.endswith("/"):
        return path
    last_segment = path.rsplit("/", 1)[-1]
    if "." in last_segment:
        return path[: len(path) - len(last_segment)]
    return path + "/"


def in_crawl_scope(url: str, root_url: str) -> bool:
    """True when `url` belongs to the crawl started at `root_url`.

    The one rule for "is this page part of that web source?": the crawler
    follows only links that pass it, and deleting a source, the orphaned
    crawl cascade and the KB-sync refresh all find a crawl's pages with it.
    If those disagreed, a page the crawler fetched would survive its
    source's deletion and be duplicated by every sync.

    Same host (ignoring a leading `www.` and the scheme) and a path at or
    under `crawl_scope_prefix(root_url)`.
    """
    try:
        candidate = urlparse(normalize_url(url))
        root = urlparse(normalize_url(root_url))
    except InvalidUrlError:
        return False
    if _host_without_www(candidate.netloc) != _host_without_www(root.netloc):
        return False
    path = candidate.path or "/"
    return path == root.path or path.startswith(crawl_scope_prefix(root.geturl()))


def _resolve_addresses(host: str) -> Iterable[IpAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return _addresses_of(infos)


def _addresses_of(infos: Iterable[tuple]) -> List[IpAddress]:
    addrs: List[IpAddress] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if addr not in addrs:
            addrs.append(addr)
    return addrs


def assert_url_is_public(url: str, *, resolve: bool = True) -> str:
    """Validate the URL and refuse to fetch internal/loopback/link-local targets.

    SSRF guard. Returns the normalized URL on success. When `resolve` is
    True (default), every resolved A/AAAA address must be a public address;
    this catches DNS rebinding setups where the hostname looks innocuous
    but resolves to an internal IP. Tests can pass `resolve=False` to skip
    the DNS step.

    A check here is advice about the moment it ran: DNS can answer
    differently by the time a request connects. The crawler's connection
    layer (`web_sources.public_transport`) re-checks with
    `resolve_public_addresses` and connects to the address it checked.

    Raises:
        InvalidUrlError: bad scheme or missing host.
        BlockedAddressError: a private/loopback/link-local address.
    """
    normalized = normalize_url(url)
    host = host_of(normalized)
    if not host:
        raise InvalidUrlError("URL must include a host")

    # Direct IP literal in the URL — check it without DNS.
    literal_ip = _ip_literal(host)
    if literal_ip is not None:
        if is_blocked_address(literal_ip):
            raise BlockedAddressError(
                "URL resolves to a non-public address; refusing to fetch."
            )
        return normalized

    if not resolve:
        return normalized

    addrs = list(_resolve_addresses(host))
    if not addrs:
        raise InvalidUrlError(f"Could not resolve host: {host}")
    for addr in addrs:
        if is_blocked_address(addr):
            raise BlockedAddressError(
                "URL resolves to a non-public address; refusing to fetch."
            )
    return normalized


def _ip_literal(host: str) -> Optional[IpAddress]:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


async def _getaddrinfo(host: str, port: int) -> List[tuple]:
    """The DNS lookup, off the event loop (the loop runs it in its executor).

    A seam: tests replace it to make a hostname resolve where they need.
    """
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def resolve_public_addresses(host: str, port: int) -> List[IpAddress]:
    """Resolve `host` and return its addresses, refusing any non-public one.

    The caller connects to one of the returned addresses rather than
    resolving the name again, so a resolver that answers with a public
    address for the check and a private one for the connect has nothing to
    swap. A name with any non-public address is refused outright, the same
    rule `assert_url_is_public` applies.

    Raises:
        BlockedAddressError: the host is, or resolves to, a non-public address.
        InvalidUrlError: the host does not resolve.
    """
    literal_ip = _ip_literal(host)
    if literal_ip is not None:
        if is_blocked_address(literal_ip):
            raise BlockedAddressError(f"Refusing to connect to non-public address {literal_ip}")
        return [literal_ip]
    try:
        infos = await _getaddrinfo(host, port)
    except socket.gaierror as e:
        raise InvalidUrlError(f"Could not resolve host: {host}") from e
    addrs = _addresses_of(infos)
    if not addrs:
        raise InvalidUrlError(f"Could not resolve host: {host}")
    for addr in addrs:
        if is_blocked_address(addr):
            raise BlockedAddressError(f"{host} resolves to a non-public address; refusing to connect")
    return addrs


# NAT64's well-known prefix embeds an IPv4 address in the low 32 bits, so
# 64:ff9b::a9fe:a9fe reaches 169.254.169.254 through a NAT64 gateway.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def is_blocked_address(addr: IpAddress) -> bool:
    """True for any address a crawl must never connect to.

    Anything not globally routable (RFC 1918, loopback, link-local — which
    holds the EC2 and ECS metadata endpoints — carrier-grade NAT, reserved
    and documentation ranges), plus multicast, and an IPv6 address that
    embeds a blocked IPv4 one.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_blocked_address(addr.ipv4_mapped)
        if addr in _NAT64_PREFIX:
            return is_blocked_address(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def url_extension_hint(url: str) -> str:
    """Return a filename hint extracted from a URL's path.

    Used to derive the document's display name when the page lacks a
    `<title>`. Always returns *something* non-empty so the SPA's document
    row is never blank.
    """
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if segments:
        return segments[-1]
    return parsed.netloc or "page"


def absolute_url(base: str, link: str) -> Optional[Tuple[str, str]]:
    """Resolve a link relative to `base` and normalize it.

    Returns (normalized_url, raw_resolved) or None when the link is not
    http(s) after resolution. Fragments and javascript:/mailto: links are
    stripped before validation.
    """
    from urllib.parse import urljoin

    if not link:
        return None
    link = link.strip()
    if not link or link.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    resolved = urljoin(base, link)
    try:
        normalized = normalize_url(resolved)
    except InvalidUrlError:
        return None
    return normalized, resolved
