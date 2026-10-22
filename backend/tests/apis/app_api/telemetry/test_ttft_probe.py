"""Scheduled time-to-first-token probe (docs/specs/turn-path-ttft.md §6E)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from apis.app_api.telemetry import ttft_probe as probe
from apis.shared.harness.runner import RUNTIME_SESSION_ID_HEADER, runtime_session_id_for

URL = "https://runtime.example/invocations"
SSE = (
    "event: message_start\ndata: {}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"text\",\"text\":\"PONG\"}\n\n"
    "event: done\ndata: {}\n\n"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _runtime(seen: List[Dict[str, Any]], *, fail_turns: bool = False, no_delta: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"body": body, "pin": request.headers.get(RUNTIME_SESSION_ID_HEADER), "auth": request.headers.get("Authorization")})
        if body.get("warm"):
            return httpx.Response(200, json={"warmed": True})
        if fail_turns:
            return httpx.Response(424, text="dependency failed")
        text = "event: message_start\ndata: {}\n\nevent: done\ndata: {}\n\n" if no_delta else SSE
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    return handler


def _run(handler, model_id=None):
    async def go():
        async with _client(handler) as client:
            return await probe.run_probe("probe-user", model_id, invocations_url=URL, bearer="tok", client=client, think_seconds=0)

    return asyncio.run(go())


def test_measures_cold_follow_up_and_warmed_turns_on_two_pinned_sessions() -> None:
    seen: List[Dict[str, Any]] = []
    result, created = _run(_runtime(seen), model_id="us.anthropic.claude-haiku-4-5")

    assert result.complete, result.errors
    assert all(isinstance(v, int) and v >= 0 for v in (result.cold_ms, result.follow_up_ms, result.warmed_ms, result.warm_call_ms))
    cold, warmed = created
    assert cold.startswith(probe.PROBE_SESSION_PREFIX) and warmed.startswith(probe.PROBE_SESSION_PREFIX) and cold != warmed

    # cold turn, follow-up on the same session, then warm + turn on the second.
    assert [(s["body"]["session_id"], bool(s["body"].get("warm"))) for s in seen] == [
        (cold, False), (cold, False), (warmed, True), (warmed, False),
    ]
    # Pinned exactly as app-api pins a user's turns, so the warm and the turn share a microVM.
    assert [s["pin"] for s in seen] == [runtime_session_id_for(sid, "probe-user") for sid in (cold, cold, warmed, warmed)]
    assert all(s["auth"] == "Bearer tok" for s in seen)
    turns = [s["body"] for s in seen if not s["body"].get("warm")]
    assert all(t["message"] == probe.PROBE_PROMPT and t["enabled_tools"] == [] for t in turns)
    assert all(t["model_id"] == "us.anthropic.claude-haiku-4-5" for t in turns)


def test_without_a_pinned_model_the_turns_send_none() -> None:
    seen: List[Dict[str, Any]] = []
    _run(_runtime(seen))
    assert all("model_id" not in s["body"] for s in seen)


def test_a_failing_turn_is_recorded_not_raised() -> None:
    result, created = _run(_runtime([], fail_turns=True))
    assert not result.complete
    assert result.cold_ms is None and result.warmed_ms is None
    assert any("HTTP 424" in e for e in result.errors)
    assert len(created) == 2  # still cleaned up


def test_a_turn_without_a_delta_is_a_failure() -> None:
    result, _ = _run(_runtime([], no_delta=True))
    assert not result.complete
    assert any("no content_block_delta" in e for e in result.errors)


def test_emit_records_measurements_and_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: seen.append(kw))
    probe.emit(probe.ProbeResult(cold_ms=3900, follow_up_ms=1400, warmed_ms=2300, warm_call_ms=2100), "m1")
    (record,) = seen
    assert record["namespace"] == "AgentCoreStack/TurnLatency"
    assert record["metrics"] == {
        "TtftProbeColdFirstTokenMs": 3900,
        "TtftProbeFollowUpFirstTokenMs": 1400,
        "TtftProbeWarmedFirstTokenMs": 2300,
        "TtftProbeWarmCallMs": 2100,
        "TtftProbeRuns": 1,
    }
    assert record["units"]["TtftProbeRuns"] == "Count"
    assert record["units"]["TtftProbeColdFirstTokenMs"] == "Milliseconds"
    assert record["properties"]["source"] == "probe"


def test_emit_counts_a_failure_and_keeps_what_it_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: seen.append(kw))
    probe.emit(probe.ProbeResult(cold_ms=3900, errors=["follow-up: HTTP 500"]), None)
    (record,) = seen
    assert record["metrics"] == {"TtftProbeColdFirstTokenMs": 3900, "TtftProbeFailures": 1}
    assert "TtftProbeRuns" not in record["metrics"]
    assert record["properties"]["modelId"] == "user-default"


def test_main_refuses_without_a_probe_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTFT_PROBE_USER_ID", raising=False)
    assert asyncio.run(probe.main_async()) == 2


def test_main_reports_an_auth_failure_as_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTFT_PROBE_USER_ID", "probe-user")
    monkeypatch.setenv("INFERENCE_API_URL", "https://runtime.example")

    class NoGrant:
        async def mint_bearer_for_user(self, user_id: str) -> str:
            raise RuntimeError("No active headless grant")

    emitted: List[Dict[str, Any]] = []
    monkeypatch.setattr(probe, "CognitoRefreshBearerAuth", NoGrant)
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: emitted.append(kw))
    assert asyncio.run(probe.main_async()) == 1
    assert emitted[0]["metrics"] == {"TtftProbeFailures": 1}
    assert "headless grant" in json.dumps(emitted[0]["properties"]["errors"])


def test_main_deletes_the_sessions_it_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTFT_PROBE_USER_ID", "probe-user")
    monkeypatch.setenv("INFERENCE_API_URL", "https://runtime.example")

    class Grant:
        async def mint_bearer_for_user(self, user_id: str) -> str:
            return "tok"

    deleted: List[str] = []

    async def fake_delete(user_id: str, session_ids: List[str]) -> None:
        deleted.extend(session_ids)

    async def fake_run(*args, **kwargs):
        return probe.ProbeResult(cold_ms=1, follow_up_ms=1, warmed_ms=1, warm_call_ms=1), ["ttftprobe-a", "ttftprobe-b"]

    monkeypatch.setattr(probe, "CognitoRefreshBearerAuth", Grant)
    monkeypatch.setattr(probe, "run_probe", fake_run)
    monkeypatch.setattr(probe, "_delete_sessions", fake_delete)
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: None)
    assert asyncio.run(probe.main_async()) == 0
    assert deleted == ["ttftprobe-a", "ttftprobe-b"]
