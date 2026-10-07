"""Runtime session affinity — pinning a conversation to one microVM.

AgentCore routes an invocation to a microVM by runtime session id. Nothing
forwarded it, so AWS assigned a fresh one per call and every turn landed on a
possibly-different container — where inference-api's in-process agent cache is
cold by definition.

Measured in dev (docs/specs/agent-cache-extra-tools-bypass.md §6 read):

    unpinned   agent_cache miss/miss/miss/miss   turns ~7.5-8.1s
    pinned     agent_cache miss/hit/hit/hit      turns ~3.1s

Both arms produced an identical prompt-cache split (write:read 0.336), so this
is a **latency** fix, not a cost one. The first probe appeared to show a cost
win too; that was run-order confound — the second arm inherited the first's
Bedrock entry because both primed with byte-identical text.
"""

import pytest

from apis.shared.harness.runner import (
    AGENTCORE_SESSION_AFFINITY_ENABLED_ENV,
    RUNTIME_SESSION_ID_HEADER,
    apply_runtime_session_header,
    legacy_runtime_session_id_for,
    runtime_session_affinity_enabled,
    runtime_session_id_for,
)


class TestRuntimeSessionId:
    def test_is_stable_for_a_session(self):
        # The entire mechanism: affinity requires byte-identical values across
        # turns. A value that varied would pin nothing.
        assert runtime_session_id_for("sess-1", "u1") == runtime_session_id_for("sess-1", "u1")

    def test_differs_between_sessions(self):
        assert runtime_session_id_for("sess-1", "u1") != runtime_session_id_for("sess-2", "u1")

    @pytest.mark.parametrize(
        "session_id",
        [
            "s",                                    # shorter than the minimum
            "exp-ceiling-6cbf29172f48",             # a short prefixed id
            "c94a3172-e1fb-4a1d-b375-6e51a56c75ad",  # a UUID
            "sess with spaces/and-slashes",          # outside the charset
        ],
    )
    def test_always_satisfies_the_agentcore_constraints(self, session_id):
        # AgentCore requires 33..128 chars from a restricted charset, and our
        # session ids meet neither reliably — which is why this hashes rather
        # than passes through.
        value = runtime_session_id_for(session_id, "u1")
        assert 33 <= len(value) <= 128
        assert all(c.isalnum() or c in "-_" for c in value)

    def test_two_users_on_one_session_id_never_share_a_runtime_session(self):
        # The whole point of per-user pinning. A session id alone does not name
        # a conversation (Memory scopes history by actor), and a shared runtime
        # session means a shared container and agent cache. On dev in
        # 2026-08 that let one user read another's live conversation.
        assert runtime_session_id_for("sess-1", "user-a") != runtime_session_id_for("sess-1", "user-b")

    def test_the_separator_keeps_the_pair_unambiguous(self):
        assert runtime_session_id_for("c", "ab") != runtime_session_id_for("bc", "a")

    def test_legacy_id_is_the_old_session_only_hash(self):
        # Only the feedback-eval look-back reads it; it must match what shipped.
        import hashlib

        expected = "sid-" + hashlib.sha256(b"sess-1").hexdigest()
        assert legacy_runtime_session_id_for("sess-1") == expected
        assert legacy_runtime_session_id_for("sess-1") != runtime_session_id_for("sess-1", "u1")

    def test_does_not_embed_the_session_id(self):
        # Keeps our identifiers out of an AWS-side one.
        assert "c94a3172" not in runtime_session_id_for("c94a3172-e1fb-4a1d", "u1")


class TestApplyRuntimeSessionHeader:
    def test_sets_the_header_for_a_known_session(self):
        headers = apply_runtime_session_header({"Content-Type": "application/json"}, "s1", "u1")
        assert headers[RUNTIME_SESSION_ID_HEADER] == runtime_session_id_for("s1", "u1")

    def test_leaves_existing_headers_alone(self):
        headers = apply_runtime_session_header({"Authorization": "Bearer x"}, "s1", "u1")
        assert headers["Authorization"] == "Bearer x"

    @pytest.mark.parametrize("session_id", [None, ""])
    def test_unknown_session_degrades_to_the_old_behavior(self, session_id):
        # Not an error: an unpinned turn is exactly what shipped before.
        headers = apply_runtime_session_header({}, session_id, "u1")
        assert RUNTIME_SESSION_ID_HEADER not in headers

    @pytest.mark.parametrize("user_id", [None, ""])
    def test_unknown_user_is_never_pinned(self, user_id):
        # Pinning without the user is the cross-user hazard; going unpinned is
        # only slower.
        headers = apply_runtime_session_header({}, "s1", user_id)
        assert RUNTIME_SESSION_ID_HEADER not in headers

    def test_kill_switch_restores_per_call_runtime_sessions(self, monkeypatch):
        monkeypatch.setenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, "false")
        headers = apply_runtime_session_header({}, "s1", "u1")
        assert RUNTIME_SESSION_ID_HEADER not in headers

    def test_empty_flag_value_stays_enabled(self, monkeypatch):
        # Workflow env vars can materialize as "" — that must not read as off.
        monkeypatch.setenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, "")
        assert runtime_session_affinity_enabled() is True
        assert RUNTIME_SESSION_ID_HEADER in apply_runtime_session_header({}, "s1", "u1")

    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, raising=False)
        assert runtime_session_affinity_enabled() is True
