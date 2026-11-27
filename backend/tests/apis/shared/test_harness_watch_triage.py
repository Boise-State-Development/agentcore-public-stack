"""``run_watch_triage`` (docs/specs/quiet-watcher-triage.md §4.1, §2.5's last row).

It sends the probe on the run's own session with the full run's tool selection fields and the
same bearer and Runtime session header, reads the one ``watch_triage_result`` frame, and turns
every failure (an HTTP error, a dropped connection, a stream with no frame, a frame it can't
trust) into an escalation, never an exception. Only a credential it can't mint raises, as for
the full run.
"""

from __future__ import annotations

import json

import httpx
import pytest

import apis.shared.harness.runner as runner_module
from apis.shared.harness.auth import HeadlessAuthError, StaticBearerAuth
from apis.shared.harness.runner import RUNTIME_SESSION_ID_HEADER, run_watch_triage
from apis.shared.scheduled_prompts.watch_triage import WatchTriageRequest

pytestmark = pytest.mark.asyncio

FP = "ab" * 16
REQUEST = WatchTriageRequest(
    schedule_id="sched-1",
    calls=[{"name": "whoami", "input": "{}"}],
    baseline=[FP],
    fingerprint_version=1,
)


def _sse(*events) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        runner_module, "_build_http_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def _triage(**overrides):
    kwargs = dict(
        user_id="user-1",
        session_id="headless-0123456789abcdef",
        request=REQUEST,
        auth=StaticBearerAuth("bearer-1"),
        enabled_tools=["whoami"],
        invocations_base_url="http://runtime",
    )
    kwargs.update(overrides)
    return await run_watch_triage(**kwargs)


async def test_it_sends_the_probe_and_reads_the_verdict(monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        sent["headers"] = request.headers
        frame = {"type": "watch_triage_result", "verdict": "quiet", "tier": 1, "reason": "unchanged", "fingerprints": [FP]}
        return httpx.Response(200, content=_sse(("watch_triage_result", frame), ("done", {})))

    _mock_http(monkeypatch, handler)
    verdict, ms = await _triage(rag_assistant_id="agent-1")

    assert (verdict.verdict, verdict.reason, verdict.tier, verdict.fingerprints) == ("quiet", "unchanged", 1, [FP])
    assert ms >= 0
    assert sent["body"] == {
        "session_id": "headless-0123456789abcdef",
        "watch_triage": {
            "scheduleId": "sched-1",
            "calls": [{"name": "whoami", "input": "{}"}],
            "baseline": [FP],
            "fingerprintVersion": 1,
        },
        "rag_assistant_id": "agent-1",
        "enabled_tools": ["whoami"],
    }
    # No message, no run_mode: the request is a triage, never a turn.
    assert "message" not in sent["body"] and "run_mode" not in sent["body"]
    assert sent["headers"]["Authorization"] == "Bearer bearer-1"
    assert sent["headers"][RUNTIME_SESSION_ID_HEADER]


async def test_an_http_error_escalates(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(503, content=b"busy"))
    verdict, _ = await _triage()
    assert (verdict.verdict, verdict.reason) == ("escalate", "triage_failed")


async def test_a_dropped_connection_escalates(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    _mock_http(monkeypatch, handler)
    verdict, _ = await _triage()
    assert (verdict.verdict, verdict.reason) == ("escalate", "triage_failed")


async def test_a_stream_with_no_verdict_escalates(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(200, content=_sse(("done", {}))))
    verdict, _ = await _triage()
    assert (verdict.verdict, verdict.reason) == ("escalate", "triage_failed")


async def test_a_verdict_it_cannot_trust_escalates(monkeypatch):
    frame = {"verdict": "quiet", "reason": "probe_error", "tier": None, "fingerprints": []}
    _mock_http(monkeypatch, lambda request: httpx.Response(200, content=_sse(("watch_triage_result", frame))))
    verdict, _ = await _triage()
    assert (verdict.verdict, verdict.reason) == ("escalate", "triage_failed")


async def test_a_credential_it_cannot_mint_raises(monkeypatch):
    class _NoGrant:
        async def mint_bearer_for_user(self, user_id):
            raise HeadlessAuthError("no grant")

    _mock_http(monkeypatch, lambda request: pytest.fail("nothing is sent without a bearer"))
    with pytest.raises(HeadlessAuthError):
        await _triage(auth=_NoGrant())
