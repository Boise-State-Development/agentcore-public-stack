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


# --- the prepare leg (flag-gated, last) -------------------------------------------------


def _prepare_runtime(seen: List[Dict[str, Any]], *, answer=None, prepare_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"body": body, "pin": request.headers.get(RUNTIME_SESSION_ID_HEADER)})
        if body.get("prepare"):
            if prepare_status >= 400:
                return httpx.Response(prepare_status, text="no")
            return httpx.Response(200, json=answer if answer is not None else {"warmed": True, "prepared": True, "reason": None, "cacheHit": False, "ms": 700})
        if body.get("warm"):
            return httpx.Response(200, json={"warmed": True})
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    return handler


def _run_prepare(handler, model_id="us.anthropic.claude-haiku-4-5"):
    async def go():
        async with _client(handler) as client:
            return await probe.run_probe(
                "probe-user", model_id, invocations_url=URL, bearer="tok", client=client, think_seconds=0, prepare_leg=True
            )

    return asyncio.run(go())


def test_flag_off_sends_the_same_requests_and_metrics_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_PREWARM_PREPARE_ENABLED", raising=False)
    monkeypatch.delenv("SESSION_PREWARM_ENABLED", raising=False)
    seen: List[Dict[str, Any]] = []
    result, created = _run(_runtime(seen), model_id="m1")
    assert len(created) == 2 and len(seen) == 4
    assert not any("prepare" in s["body"] for s in seen)
    assert [s["body"] for s in seen if s["body"].get("warm")] == [{"session_id": created[1], "warm": True}]
    assert result.prepare is None and result.prepared_ms is None and result.prepare_call_ms is None
    assert set(result.log_fields()) == {
        "cold_ms", "follow_up_ms", "warmed_ms", "warm_call_ms", "errors", "complete",
    }

    emitted: List[Dict[str, Any]] = []
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: emitted.append(kw))
    probe.emit(result, "m1")
    assert not any("Prepar" in name for name in emitted[0]["metrics"])
    assert set(emitted[0]["properties"]) == {"source", "modelId", "errors"}


def test_flag_on_prepares_a_third_conversation_with_the_turns_own_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", "true")
    seen: List[Dict[str, Any]] = []

    async def go():
        async with _client(_prepare_runtime(seen)) as client:
            return await probe.run_probe("probe-user", "m1", invocations_url=URL, bearer="tok", client=client, think_seconds=0)

    result, created = asyncio.run(go())
    assert result.complete, result.errors
    assert len(created) == 3 and len(set(created)) == 3
    cold, warmed, prepared = created
    assert [(s["body"]["session_id"], bool(s["body"].get("warm")), bool(s["body"].get("prepare"))) for s in seen] == [
        (cold, False, False), (cold, False, False), (warmed, True, False), (warmed, False, False),
        (prepared, True, True), (prepared, False, False),
    ]
    prepare_body, turn_body = seen[4]["body"], seen[5]["body"]
    assert "message" not in prepare_body
    selection = {k: v for k, v in turn_body.items() if k not in ("session_id", "message")}
    assert selection == probe._selection("m1") == {"enabled_tools": [], "model_id": "m1"}
    assert prepare_body == {"session_id": prepared, "warm": True, "prepare": True, **selection}
    assert turn_body["message"] == probe.PROBE_PROMPT
    assert seen[4]["pin"] == seen[5]["pin"] == runtime_session_id_for(prepared, "probe-user")
    assert isinstance(result.prepared_ms, int) and isinstance(result.prepare_call_ms, int)


def test_both_new_metrics_and_the_prepare_properties_are_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _run_prepare(_prepare_runtime([], answer={"warmed": True, "prepared": True, "reason": None, "cacheHit": True, "ms": 5}))
    emitted: List[Dict[str, Any]] = []
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: emitted.append(kw))
    probe.emit(result, "m1")
    (record,) = emitted
    assert record["metrics"]["TtftProbePreparedFirstTokenMs"] == result.prepared_ms
    assert record["metrics"]["TtftProbePrepareCallMs"] == result.prepare_call_ms
    assert record["units"]["TtftProbePreparedFirstTokenMs"] == "Milliseconds"
    assert "TtftProbeRuns" in record["metrics"]
    assert (record["properties"]["prepared"], record["properties"]["prepareReason"], record["properties"]["cacheHit"]) == (True, None, True)
    fields = result.log_fields()
    assert fields["prepared"] is True and fields["cacheHit"] is True and "prepare" not in fields


def test_prepared_false_still_sends_and_records_without_failing_the_run() -> None:
    seen: List[Dict[str, Any]] = []
    result, _ = _run_prepare(_prepare_runtime(seen, answer={"warmed": True, "prepared": False, "reason": "disabled", "cacheHit": False, "ms": 1}))
    assert result.complete, result.errors
    assert result.prepare == {"prepared": False, "reason": "disabled", "cacheHit": False}
    assert result.prepared_ms is not None and result.prepare_call_ms is not None
    assert not seen[-1]["body"].get("warm")  # the turn was still sent


def test_a_failed_prepare_call_still_sends_and_is_not_a_failure() -> None:
    seen: List[Dict[str, Any]] = []
    result, created = _run_prepare(_prepare_runtime(seen, prepare_status=500))
    assert result.complete, result.errors
    assert result.prepare_call_ms is None and result.prepared_ms is not None
    assert result.prepare is not None and result.prepare["prepared"] is False and "HTTP 500" in str(result.prepare["reason"])
    assert len(created) == 3 and not seen[-1]["body"].get("warm")


def test_a_failed_prepared_send_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("warm"):
            return httpx.Response(200, json={"warmed": True, "prepared": True})
        if body["session_id"] == handler.prepared:  # type: ignore[attr-defined]
            return httpx.Response(424, text="x")
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    handler.prepared = None  # type: ignore[attr-defined]

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("prepare"):
            handler.prepared = body["session_id"]  # type: ignore[attr-defined]
        return handler(request)

    result, _ = _run_prepare(wrapped)
    assert not result.complete and any(e.startswith("prepared: HTTP 424") for e in result.errors)


def test_main_deletes_the_prepared_conversation_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTFT_PROBE_USER_ID", "probe-user")
    monkeypatch.setenv("INFERENCE_API_URL", "https://runtime.example")

    class Grant:
        async def mint_bearer_for_user(self, user_id: str) -> str:
            return "tok"

    deleted: List[str] = []

    async def fake_delete(user_id: str, session_ids: List[str]) -> None:
        deleted.extend(session_ids)

    async def fake_run(*args, **kwargs):
        done = probe.ProbeResult(cold_ms=1, follow_up_ms=1, warmed_ms=1, warm_call_ms=1)
        return done, ["ttftprobe-a", "ttftprobe-b", "ttftprobe-c"]

    monkeypatch.setattr(probe, "CognitoRefreshBearerAuth", Grant)
    monkeypatch.setattr(probe, "run_probe", fake_run)
    monkeypatch.setattr(probe, "_delete_sessions", fake_delete)
    monkeypatch.setattr(probe, "emit_emf_metrics", lambda **kw: None)
    assert asyncio.run(probe.main_async()) == 0
    assert deleted == ["ttftprobe-a", "ttftprobe-b", "ttftprobe-c"]
