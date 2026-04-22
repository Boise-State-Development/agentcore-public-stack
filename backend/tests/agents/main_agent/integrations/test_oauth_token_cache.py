"""Tests for the in-process OAuth token cache."""

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
