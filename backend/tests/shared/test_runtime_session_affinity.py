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
    runtime_session_affinity_enabled,
    runtime_session_id_for,
)


class TestRuntimeSessionId:
    def test_is_stable_for_a_session(self):
        # The entire mechanism: affinity requires byte-identical values across
        # turns. A value that varied would pin nothing.
        assert runtime_session_id_for("sess-1") == runtime_session_id_for("sess-1")

    def test_differs_between_sessions(self):
        assert runtime_session_id_for("sess-1") != runtime_session_id_for("sess-2")

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
        value = runtime_session_id_for(session_id)
        assert 33 <= len(value) <= 128
        assert all(c.isalnum() or c in "-_" for c in value)

    def test_does_not_embed_the_session_id(self):
        # Keeps our identifiers out of an AWS-side one.
        assert "c94a3172" not in runtime_session_id_for("c94a3172-e1fb-4a1d")


class TestApplyRuntimeSessionHeader:
    def test_sets_the_header_for_a_known_session(self):
        headers = apply_runtime_session_header({"Content-Type": "application/json"}, "s1")
        assert headers[RUNTIME_SESSION_ID_HEADER] == runtime_session_id_for("s1")

    def test_leaves_existing_headers_alone(self):
        headers = apply_runtime_session_header({"Authorization": "Bearer x"}, "s1")
        assert headers["Authorization"] == "Bearer x"

    @pytest.mark.parametrize("session_id", [None, ""])
    def test_unknown_session_degrades_to_the_old_behavior(self, session_id):
        # Not an error: an unpinned turn is exactly what shipped before.
        headers = apply_runtime_session_header({}, session_id)
        assert RUNTIME_SESSION_ID_HEADER not in headers

    def test_kill_switch_restores_per_call_runtime_sessions(self, monkeypatch):
        monkeypatch.setenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, "false")
        headers = apply_runtime_session_header({}, "s1")
        assert RUNTIME_SESSION_ID_HEADER not in headers

    def test_empty_flag_value_stays_enabled(self, monkeypatch):
        # Workflow env vars can materialize as "" — that must not read as off.
        monkeypatch.setenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, "")
        assert runtime_session_affinity_enabled() is True
        assert RUNTIME_SESSION_ID_HEADER in apply_runtime_session_header({}, "s1")

    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv(AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, raising=False)
        assert runtime_session_affinity_enabled() is True
