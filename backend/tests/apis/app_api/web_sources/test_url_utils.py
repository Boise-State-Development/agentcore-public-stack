"""Pure unit tests for the URL helpers shared by the route and the crawler.

The crawler dedupes by normalized URL and gates outgoing fetches by the
SSRF guards in this module — so divergence between request-time validation
and discovery-time validation is exactly the failure mode these tests
exist to prevent.
"""

from __future__ import annotations

import pytest

from apis.app_api.web_sources.url_utils import (
    InvalidUrlError,
    absolute_url,
    assert_url_is_public,
    crawl_scope_prefix,
    host_of,
    in_crawl_scope,
    normalize_url,
    url_extension_hint,
)


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host(self):
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_drops_fragment(self):
        assert normalize_url("https://x.com/p#section") == "https://x.com/p"

    def test_strips_default_port_http(self):
        assert normalize_url("http://x.com:80/p") == "http://x.com/p"

    def test_strips_default_port_https(self):
        assert normalize_url("https://x.com:443/p") == "https://x.com/p"

    def test_preserves_non_default_port(self):
        assert normalize_url("http://x.com:8080/p") == "http://x.com:8080/p"

    def test_preserves_query(self):
        assert normalize_url("https://x.com/p?a=1&b=2") == "https://x.com/p?a=1&b=2"

    def test_supplies_root_path_when_missing(self):
        assert normalize_url("https://x.com") == "https://x.com/"

    def test_rejects_non_http_schemes(self):
        with pytest.raises(InvalidUrlError):
            normalize_url("ftp://x.com/p")
        with pytest.raises(InvalidUrlError):
            normalize_url("javascript:alert(1)")

    def test_rejects_missing_host(self):
        with pytest.raises(InvalidUrlError):
            normalize_url("https:///path")


class TestCrawlScope:
    @pytest.mark.parametrize(
        "root,prefix",
        [
            ("https://example.com/", "/"),
            ("https://example.com/research/", "/research/"),
            ("https://example.com/research", "/research/"),
            ("https://example.com/research/index.html", "/research/"),
            ("https://example.com/a/b/", "/a/b/"),
        ],
    )
    def test_prefix_is_the_roots_section(self, root: str, prefix: str):
        assert crawl_scope_prefix(root) == prefix

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/research/",
            "https://example.com/research",
            "https://example.com/research/labs/a",
            "https://www.example.com/research/labs/a",
            "http://example.com/research/b",
        ],
    )
    def test_pages_in_the_section_are_in_scope(self, url: str):
        assert in_crawl_scope(url, "https://example.com/research")

    @pytest.mark.parametrize(
        "url",
        [
            # The reported case: the site nav links back to the home page.
            "https://example.com/",
            "https://example.com/admissions/",
            "https://example.com/research-news/x",
            "https://docs.example.com/research/a",
            "https://other.com/research/a",
            "not-a-url",
        ],
    )
    def test_pages_outside_the_section_are_out_of_scope(self, url: str):
        assert not in_crawl_scope(url, "https://example.com/research/")

    def test_a_home_page_root_covers_its_host(self):
        root = "https://www.example.com/"
        assert in_crawl_scope("https://example.com/admissions/", root)
        assert not in_crawl_scope("https://docs.example.com/a", root)

    def test_root_reached_through_www_redirect_keeps_its_children(self):
        # Pages are stored under the host the crawler fetched them from, so a
        # root typed without www. must still own the site's absolute www. links.
        assert in_crawl_scope(
            "https://www.example.com/research/a", "https://example.com/research/"
        )


class TestSsrfGuard:
    def test_accepts_ip_literal_public(self):
        assert (
            assert_url_is_public("http://1.1.1.1/path", resolve=False)
            == "http://1.1.1.1/path"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://10.0.0.5/",
            "http://192.168.1.10/",
            "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
            "http://[::1]/",
            "http://[fe80::1]/",
        ],
    )
    def test_rejects_internal_ip_literals(self, url: str):
        with pytest.raises(InvalidUrlError):
            assert_url_is_public(url, resolve=False)

    def test_rejects_bad_scheme(self):
        with pytest.raises(InvalidUrlError):
            assert_url_is_public("ftp://example.com/")

    def test_resolve_false_skips_dns_check(self):
        # A non-existent host would normally fail at DNS; resolve=False
        # short-circuits that.
        assert assert_url_is_public(
            "https://definitely-not-a-real-host.example/", resolve=False
        )

    def test_resolve_true_rejects_unresolvable_host(self):
        with pytest.raises(InvalidUrlError):
            assert_url_is_public(
                "https://definitely-not-a-real-host.example.invalid/", resolve=True
            )


class TestAbsoluteUrl:
    def test_relative_resolution(self):
        result = absolute_url("https://example.com/a/", "b/c")
        assert result is not None
        normalized, raw = result
        assert normalized == "https://example.com/a/b/c"

    def test_absolute_link_kept(self):
        result = absolute_url("https://example.com/", "https://other.com/x")
        assert result is not None
        assert result[0] == "https://other.com/x"

    def test_javascript_filtered(self):
        assert absolute_url("https://x.com/", "javascript:alert(1)") is None

    def test_mailto_filtered(self):
        assert absolute_url("https://x.com/", "mailto:a@b.com") is None

    def test_empty_filtered(self):
        assert absolute_url("https://x.com/", "") is None
        assert absolute_url("https://x.com/", "#section") is None


class TestUrlExtensionHint:
    def test_extracts_last_path_segment(self):
        assert url_extension_hint("https://x.com/a/b/article") == "article"

    def test_falls_back_to_host(self):
        assert url_extension_hint("https://x.com/") == "x.com"


class TestHostOf:
    def test_lowercases(self):
        assert host_of("https://Example.COM/path") == "example.com"

    def test_handles_unparseable(self):
        assert host_of("not a url") is None


class TestIsBlockedAddress:
    @pytest.mark.parametrize(
        "address",
        [
            "169.254.169.254",  # EC2 instance metadata
            "169.254.170.2",  # ECS task metadata / credentials
            "10.1.2.3",
            "172.16.0.1",
            "192.168.0.1",
            "127.0.0.1",
            "100.64.0.1",  # carrier-grade NAT
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fd00::1",
            "fe80::1",
            "::ffff:169.254.169.254",  # IPv4-mapped
            "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        ],
    )
    def test_blocks_non_public(self, address: str):
        import ipaddress

        from apis.app_api.web_sources.url_utils import is_blocked_address

        assert is_blocked_address(ipaddress.ip_address(address))

    @pytest.mark.parametrize("address", ["8.8.8.8", "93.184.215.14", "2606:4700:4700::1111", "64:ff9b::808:808"])
    def test_allows_public(self, address: str):
        import ipaddress

        from apis.app_api.web_sources.url_utils import is_blocked_address

        assert not is_blocked_address(ipaddress.ip_address(address))


class TestResolvePublicAddresses:
    @pytest.fixture
    def resolver(self, monkeypatch: pytest.MonkeyPatch):
        from apis.app_api.web_sources import url_utils

        answers: dict = {}

        async def fake_getaddrinfo(host: str, port: int):
            if host not in answers:
                import socket

                raise socket.gaierror("no such host")
            return [(2, 1, 6, "", (a, port)) for a in answers[host]]

        monkeypatch.setattr(url_utils, "_getaddrinfo", fake_getaddrinfo)
        return answers

    @pytest.mark.asyncio
    async def test_returns_public_addresses(self, resolver):
        from apis.app_api.web_sources.url_utils import resolve_public_addresses

        resolver["example.com"] = ["93.184.215.14", "93.184.215.14"]
        assert [str(a) for a in await resolve_public_addresses("example.com", 443)] == ["93.184.215.14"]

    @pytest.mark.asyncio
    async def test_refuses_a_hostname_resolving_privately(self, resolver):
        from apis.app_api.web_sources.url_utils import BlockedAddressError, resolve_public_addresses

        resolver["intranet.example.com"] = ["10.0.0.7"]
        with pytest.raises(BlockedAddressError):
            await resolve_public_addresses("intranet.example.com", 443)

    @pytest.mark.asyncio
    async def test_unresolvable_host_is_invalid_not_blocked(self, resolver):
        from apis.app_api.web_sources.url_utils import BlockedAddressError, resolve_public_addresses

        with pytest.raises(InvalidUrlError) as excinfo:
            await resolve_public_addresses("nowhere.invalid", 443)
        assert not isinstance(excinfo.value, BlockedAddressError)

    @pytest.mark.asyncio
    async def test_ip_literals_skip_dns(self, resolver):
        from apis.app_api.web_sources.url_utils import BlockedAddressError, resolve_public_addresses

        assert [str(a) for a in await resolve_public_addresses("8.8.8.8", 80)] == ["8.8.8.8"]
        with pytest.raises(BlockedAddressError):
            await resolve_public_addresses("::1", 80)

    def test_assert_url_is_public_raises_blocked_for_literals(self):
        from apis.app_api.web_sources.url_utils import BlockedAddressError

        with pytest.raises(BlockedAddressError):
            assert_url_is_public("http://169.254.170.2/v2/credentials", resolve=False)
