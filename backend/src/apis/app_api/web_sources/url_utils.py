"""URL normalization, same-domain comparison, and SSRF guards.

Kept in one module so the route validator and the crawler share the exact
same logic — divergence between "is this URL safe to start a crawl from?"
and "is this discovered link safe to follow?" is how SSRF holes get reborn.
"""

import ipaddress
import socket
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse, urlunparse


class InvalidUrlError(ValueError):
    """The supplied string is not a URL we will fetch."""


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


def same_registrable_domain(a: str, b: str) -> bool:
    """True when `a` and `b` share a registrable domain.

    Uses a coarse last-two-labels heuristic — fine for the common cases
    (`docs.example.com` vs `example.com`) and avoids a `tldextract`
    dependency. Edge cases like `.co.uk` are treated as different domains
    than `.example.co.uk`; that's the safe direction (we'll under-crawl
    rather than wander off-site).
    """
    ha = host_of(a)
    hb = host_of(b)
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    parts_a = ha.split(".")
    parts_b = hb.split(".")
    if len(parts_a) < 2 or len(parts_b) < 2:
        return False
    return parts_a[-2:] == parts_b[-2:]


def _resolve_addresses(host: str) -> Iterable[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    addrs = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return addrs


def assert_url_is_public(url: str, *, resolve: bool = True) -> str:
    """Validate the URL and refuse to fetch internal/loopback/link-local targets.

    SSRF guard. Returns the normalized URL on success. When `resolve` is
    True (default), every resolved A/AAAA address must be a public address;
    this catches DNS rebinding setups where the hostname looks innocuous
    but resolves to an internal IP. Tests can pass `resolve=False` to skip
    the DNS step.

    Raises:
        InvalidUrlError: bad scheme, missing host, or a private/loopback
            address.
    """
    normalized = normalize_url(url)
    host = host_of(normalized)
    if not host:
        raise InvalidUrlError("URL must include a host")

    # Direct IP literal in the URL — check it without DNS.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_blocked(literal_ip):
            raise InvalidUrlError(
                "URL resolves to a non-public address; refusing to fetch."
            )
        return normalized

    if not resolve:
        return normalized

    addrs = list(_resolve_addresses(host))
    if not addrs:
        raise InvalidUrlError(f"Could not resolve host: {host}")
    for addr in addrs:
        if _is_blocked(addr):
            raise InvalidUrlError(
                "URL resolves to a non-public address; refusing to fetch."
            )
    return normalized


def _is_blocked(addr: ipaddress._BaseAddress) -> bool:
    return (
        addr.is_private
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
