"""Tests for the in-process OAuth token cache."""

import time
from unittest.mock import patch

from agents.main_agent.integrations import oauth_token_cache


def _isolate(user: str = "tester") -> None:
    oauth_token_cache.clear_user(user)


def test_get_returns_none_when_unset():
    _isolate()
    assert oauth_token_cache.get("tester", "google") is None


def test_set_then_get_roundtrip():
    _isolate()
    oauth_token_cache.set("tester", "google", "tok-1")
    assert oauth_token_cache.get("tester", "google") == "tok-1"


def test_per_user_isolation():
    oauth_token_cache.clear_user("alice")
    oauth_token_cache.clear_user("bob")
    oauth_token_cache.set("alice", "google", "alice-tok")
    oauth_token_cache.set("bob", "google", "bob-tok")

    assert oauth_token_cache.get("alice", "google") == "alice-tok"
    assert oauth_token_cache.get("bob", "google") == "bob-tok"


def test_per_provider_isolation():
    _isolate()
    oauth_token_cache.set("tester", "google", "g-tok")
    oauth_token_cache.set("tester", "github", "gh-tok")

    assert oauth_token_cache.get("tester", "google") == "g-tok"
    assert oauth_token_cache.get("tester", "github") == "gh-tok"


def test_clear_user_drops_only_that_user():
    oauth_token_cache.clear_user("alice")
    oauth_token_cache.clear_user("bob")
    oauth_token_cache.set("alice", "google", "a")
    oauth_token_cache.set("bob", "google", "b")

    removed = oauth_token_cache.clear_user("alice")

    assert removed == 1
    assert oauth_token_cache.get("alice", "google") is None
    assert oauth_token_cache.get("bob", "google") == "b"


def test_clear_user_provider_drops_only_that_pair():
    _isolate()
    oauth_token_cache.set("tester", "google", "g")
    oauth_token_cache.set("tester", "github", "gh")

    oauth_token_cache.clear_user_provider("tester", "google")

    assert oauth_token_cache.get("tester", "google") is None
    assert oauth_token_cache.get("tester", "github") == "gh"


class TestExpiry:
    """The TTL is the load-bearing piece that keeps the cache from
    handing out tokens past their upstream lifetime. Without it, the 401
    retry path used to mark the user as disconnected and force re-consent
    every ~1 hour.
    """

    def test_get_returns_none_after_ttl_elapses(self):
        _isolate()

        with patch("agents.main_agent.integrations.oauth_token_cache.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            oauth_token_cache.set("tester", "google", "tok", ttl_seconds=60)

            mock_time.monotonic.return_value = 1059.999
            assert oauth_token_cache.get("tester", "google") == "tok"

            mock_time.monotonic.return_value = 1060.0
            assert oauth_token_cache.get("tester", "google") is None

    def test_expired_entry_is_evicted_on_read(self):
        """An expired entry must not linger — a subsequent set with the
        same key should not be merged with stale state."""
        _isolate()

        with patch("agents.main_agent.integrations.oauth_token_cache.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            oauth_token_cache.set("tester", "google", "old", ttl_seconds=10)

            mock_time.monotonic.return_value = 100.0
            assert oauth_token_cache.get("tester", "google") is None
            # Internal dict no longer contains the stale entry.
            assert ("tester", "google") not in oauth_token_cache._cache

    def test_default_ttl_is_under_googles_access_token_lifetime(self):
        """3000s gives ~10 min of safety margin under Google's 3600s
        access token lifetime — we want to refresh proactively, not
        wait for the upstream 401."""
        assert oauth_token_cache.DEFAULT_TTL_SECONDS <= 3000
        # Sanity floor: a too-aggressive TTL would defeat the cache.
        assert oauth_token_cache.DEFAULT_TTL_SECONDS >= 60

    def test_set_uses_default_ttl_when_unspecified(self):
        _isolate()

        with patch("agents.main_agent.integrations.oauth_token_cache.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            oauth_token_cache.set("tester", "google", "tok")

            # Just under default TTL — still cached.
            mock_time.monotonic.return_value = (
                oauth_token_cache.DEFAULT_TTL_SECONDS - 1
            )
            assert oauth_token_cache.get("tester", "google") == "tok"

            # Past default TTL — gone.
            mock_time.monotonic.return_value = (
                oauth_token_cache.DEFAULT_TTL_SECONDS + 1
            )
            assert oauth_token_cache.get("tester", "google") is None

    def test_set_resets_expiry(self):
        """A fresh set after expiry stores a new entry with a new clock,
        so the old expiry doesn't leak through."""
        _isolate()

        with patch("agents.main_agent.integrations.oauth_token_cache.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            oauth_token_cache.set("tester", "google", "old", ttl_seconds=10)

            mock_time.monotonic.return_value = 100.0
            assert oauth_token_cache.get("tester", "google") is None

            # Re-set should not be subject to the old expiry.
            oauth_token_cache.set("tester", "google", "new", ttl_seconds=10)
            mock_time.monotonic.return_value = 105.0
            assert oauth_token_cache.get("tester", "google") == "new"

    def test_real_clock_short_ttl_expires(self):
        """End-to-end check without clock mocking — guard against the
        mock hiding a real-world bug."""
        _isolate()
        oauth_token_cache.set("tester", "google", "tok", ttl_seconds=0.05)
        assert oauth_token_cache.get("tester", "google") == "tok"
        time.sleep(0.1)
        assert oauth_token_cache.get("tester", "google") is None
