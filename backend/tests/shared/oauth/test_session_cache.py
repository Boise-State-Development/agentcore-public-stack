"""Tests for the OAuth consent session cache."""

from __future__ import annotations

import time

import pytest

from apis.shared.oauth import session_cache


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Cache is module-global; drop all entries around each test."""
    session_cache.forget_user("alice")
    session_cache.forget_user("bob")
    # Also clear anything else left over from prior tests by bumping
    # the clock forward inside a prune.
    session_cache._pending.clear()  # noqa: SLF001 — test-only
    yield
    session_cache._pending.clear()  # noqa: SLF001 — test-only


class TestRememberAndConsume:
    def test_consume_matches_remembered_pair(self):
        session_cache.remember("alice", "uri-123")
        assert session_cache.consume("alice", "uri-123") is True

    def test_consume_is_single_use(self):
        session_cache.remember("alice", "uri-123")
        assert session_cache.consume("alice", "uri-123") is True
        # Second consume on the same uri fails — no replay.
        assert session_cache.consume("alice", "uri-123") is False

    def test_consume_rejects_mismatched_user(self):
        session_cache.remember("alice", "uri-123")
        # Bob cannot complete a session Alice started.
        assert session_cache.consume("bob", "uri-123") is False
        # Alice's entry still there — rejection is non-destructive.
        assert session_cache.consume("alice", "uri-123") is True

    def test_consume_rejects_unknown_session(self):
        assert session_cache.consume("alice", "never-seen") is False

    def test_remember_overwrites_previous_owner(self):
        # If a second user somehow remembers the same uri, the latest
        # write wins. (AgentCore request_uris are opaque and unique in
        # practice — this is just a deterministic-semantics test.)
        session_cache.remember("alice", "uri-123")
        session_cache.remember("bob", "uri-123")
        assert session_cache.consume("alice", "uri-123") is False
        assert session_cache.consume("bob", "uri-123") is True


class TestExpiry:
    def test_expired_entries_are_rejected(self, monkeypatch):
        # Freeze clock: remember at t=0, consume at t=TTL+1.
        t = [0.0]
        monkeypatch.setattr(session_cache.time, "monotonic", lambda: t[0])

        session_cache.remember("alice", "uri-123")
        t[0] = session_cache._TTL_SECONDS + 1.0  # noqa: SLF001 — test-only
        assert session_cache.consume("alice", "uri-123") is False

    def test_entries_within_ttl_succeed(self, monkeypatch):
        t = [0.0]
        monkeypatch.setattr(session_cache.time, "monotonic", lambda: t[0])

        session_cache.remember("alice", "uri-123")
        t[0] = session_cache._TTL_SECONDS - 1.0  # noqa: SLF001 — test-only
        assert session_cache.consume("alice", "uri-123") is True

    def test_prune_evicts_expired_entries(self, monkeypatch):
        t = [0.0]
        monkeypatch.setattr(session_cache.time, "monotonic", lambda: t[0])

        session_cache.remember("alice", "old-uri")
        t[0] = session_cache._TTL_SECONDS + 1.0  # noqa: SLF001 — test-only
        # Any subsequent remember/consume triggers the internal prune.
        session_cache.remember("bob", "new-uri")
        assert "old-uri" not in session_cache._pending  # noqa: SLF001 — test-only


class TestForgetUser:
    def test_forget_clears_only_that_user(self):
        session_cache.remember("alice", "a1")
        session_cache.remember("alice", "a2")
        session_cache.remember("bob", "b1")

        assert session_cache.forget_user("alice") == 2
        assert session_cache.consume("alice", "a1") is False
        assert session_cache.consume("alice", "a2") is False
        assert session_cache.consume("bob", "b1") is True


class TestExtractSessionUri:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest-uri%3Aabc123&client_id=x",
                "urn:ietf:params:oauth:request-uri:abc123",
            ),
            (
                "https://example.com/authorize?session_id=sess_abc123&x=1",
                "sess_abc123",
            ),
            (
                "https://example.com/authorize?sessionUri=sess_xyz",
                "sess_xyz",
            ),
            (
                "https://example.com/authorize?sessionId=sess_xyz",
                "sess_xyz",
            ),
        ],
    )
    def test_extracts_known_params(self, url, expected):
        assert session_cache.extract_session_uri(url) == expected

    def test_returns_none_when_no_known_param(self):
        url = "https://example.com/authorize?client_id=x&state=y"
        assert session_cache.extract_session_uri(url) is None

    def test_returns_none_for_empty_input(self):
        assert session_cache.extract_session_uri("") is None
        assert session_cache.extract_session_uri(None) is None  # type: ignore[arg-type]

    def test_prefers_request_uri_when_multiple_present(self):
        # If AgentCore ever emits both, request_uri is the canonical OAuth 2.0
        # PAR parameter — prefer it.
        url = "https://example.com/authorize?session_id=shouldnt_win&request_uri=should_win"
        assert session_cache.extract_session_uri(url) == "should_win"

    def test_handles_malformed_url(self):
        # parse_qs is lenient, but urlparse should still not raise on junk.
        assert session_cache.extract_session_uri("not a url") is None
