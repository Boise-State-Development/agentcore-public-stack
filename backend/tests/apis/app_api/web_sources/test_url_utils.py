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
    host_of,
    normalize_url,
    same_registrable_domain,
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


class TestSameRegistrableDomain:
    def test_identical_hosts(self):
        assert same_registrable_domain(
            "https://example.com/a", "https://example.com/b"
        )

    def test_subdomain_matches_apex(self):
        assert same_registrable_domain(
            "https://docs.example.com/a", "https://example.com/b"
        )

    def test_different_apex(self):
        assert not same_registrable_domain(
            "https://example.com/a", "https://other.com/b"
        )

    def test_handles_unparseable(self):
        assert not same_registrable_domain("not-a-url", "https://example.com")


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
