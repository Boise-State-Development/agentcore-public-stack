"""The ``watch_triage`` invocation (docs/specs/quiet-watcher-triage.md §2.5, §4.1).

Every §2.5 failure row the Runtime meets escalates: a probe tool no longer in the run's tool set
or no longer read-only is ``probe_refused``; a replay that raises, times out or returns an error
is ``probe_error``; a resolution that fails is ``probe_error``; the flag off is ``flag_off``.
Matching fingerprints are quiet and any difference escalates (no decider until T.3). The calls
replay concurrently, and the stream is one frame then ``done``. The route sends a triage request
here, never to a turn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.watch_triage import (
    replay_call,
    run_watch_triage,
    stream_watch_triage,
    triage_probe,
)
from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION, fingerprint_result
from apis.shared.scheduled_prompts.watch_triage import WatchTriageRequest

pytestmark = pytest.mark.asyncio


def _ok(text: str) -> dict:
    return {"status": "success", "content": [{"text": text}]}


class _Tool:
    """A loaded tool by its model-facing name; ``runs_in_parallel`` judges it by that name."""

    def __init__(self, name: str) -> None:
        self.tool_name = name


def _request(*names_and_results) -> WatchTriageRequest:
    return WatchTriageRequest(
        schedule_id="sched-1",
        calls=[{"name": name, "input": json.dumps({"q": i})} for i, (name, _) in enumerate(names_and_results)],
        baseline=[fingerprint_result(result) for _, result in names_and_results],
        fingerprint_version=FINGERPRINT_VERSION,
    )


def _calls_returning(results_by_name):
    seen = []

    async def call_tool(tool, arguments, tool_use_id):
        seen.append((tool.tool_name, arguments))
        outcome = results_by_name[tool.tool_name]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return call_tool, seen


TOOLS = {"whoami": _Tool("whoami"), "calculator": _Tool("calculator"), "writer": _Tool("create_issue")}


async def test_matching_fingerprints_are_quiet_with_the_stored_arguments():
    request = _request(("whoami", _ok("me")), ("calculator", _ok("4")))
    call_tool, seen = _calls_returning({"whoami": _ok("me"), "calculator": _ok("4")})

    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)

    assert (verdict.verdict, verdict.reason, verdict.tier) == ("quiet", "unchanged", 1)
    assert sorted(seen) == [("calculator", {"q": 1}), ("whoami", {"q": 0})]


async def test_a_changed_result_escalates():
    request = _request(("whoami", _ok("me")))
    call_tool, _ = _calls_returning({"whoami": _ok("someone else")})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason, verdict.tier) == ("escalate", "changed_no_decider", 1)


async def test_a_probe_tool_gone_from_the_tool_set_is_refused():
    request = _request(("whoami", _ok("me")), ("github___list_notifications", _ok("[]")))
    call_tool, seen = _calls_returning({"whoami": _ok("me")})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_refused")
    assert seen == []  # refused before anything replays


async def test_a_probe_tool_no_longer_read_only_is_refused():
    request = _request(("writer", _ok("done")))
    tools = {"writer": _Tool("writer")}
    call_tool, seen = _calls_returning({"writer": _ok("done")})
    verdict = await triage_probe(request, tools, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_refused")
    assert seen == []


async def test_a_replay_that_raises_escalates():
    request = _request(("whoami", _ok("me")), ("calculator", _ok("4")))
    call_tool, _ = _calls_returning({"whoami": RuntimeError("server down"), "calculator": _ok("4")})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")
    assert verdict.fingerprints[0] is None and verdict.fingerprints[1] == fingerprint_result(_ok("4"))


async def test_a_replay_that_times_out_escalates():
    request = _request(("whoami", _ok("me")))

    async def slow(tool, arguments, tool_use_id):
        await asyncio.sleep(5)
        return _ok("me")

    verdict = await triage_probe(request, TOOLS, call_tool=slow, timeout_seconds=0.01)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")


async def test_a_replay_that_returns_an_error_escalates_even_if_it_matches():
    error = {"status": "error", "content": [{"text": "rate limited"}]}
    request = _request(("whoami", error))
    call_tool, _ = _calls_returning({"whoami": error})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")


async def test_a_replay_with_no_result_escalates():
    request = _request(("whoami", _ok("me")))
    call_tool, _ = _calls_returning({"whoami": None})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")


async def test_a_version_mismatch_is_refused():
    request = _request(("whoami", _ok("me"))).model_copy(update={"fingerprint_version": FINGERPRINT_VERSION + 1})
    call_tool, seen = _calls_returning({"whoami": _ok("me")})
    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_refused")
    assert seen == []


async def test_the_calls_replay_concurrently():
    request = _request(("whoami", _ok("me")), ("calculator", _ok("4")))
    started, release = [], asyncio.Event()

    async def call_tool(tool, arguments, tool_use_id):
        started.append(tool.tool_name)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 1)  # a sequential replay would time out here
        return _ok("me") if tool.tool_name == "whoami" else _ok("4")

    verdict = await triage_probe(request, TOOLS, call_tool=call_tool)
    assert verdict.reason == "unchanged"


async def test_a_real_tool_replays_to_a_stable_fingerprint():
    """``replay_call`` streams a Strands tool as the executor does; the tool use id never reaches the hash."""
    from strands import tool

    @tool
    def whoami() -> dict:
        """Who the caller is."""
        return {"status": "success", "content": [{"text": "Avery Example"}]}

    first = await replay_call(whoami, {}, "triage-1")
    second = await replay_call(whoami, {}, "triage-2")
    assert first["toolUseId"] == "triage-1" and second["toolUseId"] == "triage-2"
    assert fingerprint_result(first) == fingerprint_result(second) == fingerprint_result(_ok("Avery Example"))


# --- Resolution, cleanup and the stream --------------------------------------------------------


class _Registry:
    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


async def test_resolution_feeds_the_loader_and_the_registry_is_released():
    request = _request(("whoami", _ok("me")))
    registry = _Registry()
    loaded = {}

    def load(**kwargs):
        loaded.update(kwargs)
        return {"whoami": _Tool("whoami")}, registry, []

    async def resolve():
        return {"user_id": "u", "auth_token": "t", "enabled_tools": ["whoami"], "extra_tools": []}

    call_tool, _ = _calls_returning({"whoami": _ok("me")})
    verdict = await run_watch_triage(request, resolve_inputs=resolve, load=load, call_tool=call_tool)
    assert verdict.reason == "unchanged"
    assert loaded["enabled_tools"] == ["whoami"]
    assert registry.cleaned


async def test_a_turn_that_would_not_run_is_refused():
    async def resolve():
        return None

    verdict = await run_watch_triage(_request(("whoami", _ok("me"))), resolve_inputs=resolve)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_refused")


async def test_a_resolution_that_fails_escalates_and_still_releases():
    registry = _Registry()

    def load(**kwargs):
        return {"whoami": _Tool("whoami")}, registry, []

    async def resolve():
        return {"user_id": "u", "auth_token": None, "enabled_tools": [], "extra_tools": []}

    async def explode(tool, arguments, tool_use_id):
        raise asyncio.CancelledError  # a BaseException from deep in a client

    verdict = await run_watch_triage(
        _request(("whoami", _ok("me"))), resolve_inputs=resolve, load=load, call_tool=explode
    )
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")
    assert registry.cleaned


async def test_a_loader_that_raises_escalates():
    def load(**kwargs):
        raise RuntimeError("catalog unreachable")

    async def resolve():
        return {"user_id": "u", "auth_token": None, "enabled_tools": [], "extra_tools": []}

    verdict = await run_watch_triage(_request(("whoami", _ok("me"))), resolve_inputs=resolve, load=load)
    assert (verdict.verdict, verdict.reason) == ("escalate", "probe_error")


async def test_the_flag_off_streams_flag_off_then_done_and_resolves_nothing():
    resolved = []

    async def resolve():
        resolved.append(True)
        return None

    frames = [f async for f in stream_watch_triage(_request(("whoami", _ok("me"))), enabled=False, resolve_inputs=resolve)]
    assert resolved == []
    assert frames[0].startswith("event: watch_triage_result\n")
    assert json.loads(frames[0].split("data: ", 1)[1])["reason"] == "flag_off"
    assert frames[1].startswith("event: done\n") and len(frames) == 2


def _invocation(session_id: str) -> InvocationRequest:
    return InvocationRequest(
        session_id=session_id,
        watch_triage=_request(("whoami", _ok("me"))).model_dump(by_alias=True),
        enabled_tools=["whoami"],
    )


async def test_the_dispatcher_sends_a_triage_request_to_the_triage_never_a_turn(monkeypatch):
    seen = []

    def fake_triage(request, current_user):
        seen.append(request.session_id)
        return "triage"

    async def no_turn(*args, **kwargs):
        raise AssertionError("a triage request must not run a turn")

    monkeypatch.setattr(routes, "_watch_triage_invocation", fake_triage)
    monkeypatch.setattr(routes, "_run_invocation", no_turn)
    assert await routes.invocations(_invocation("headless-abc"), current_user=object()) == "triage"
    assert seen == ["headless-abc"]


async def _body(response) -> list:
    return [chunk async for chunk in response.body_iterator]


async def test_a_triage_on_an_interactive_session_answers_flag_off(monkeypatch):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.setenv("WATCH_TRIAGE_ENABLED", "true")
    response = routes._watch_triage_invocation(_invocation("web-session-1"), current_user=object())
    frames = await _body(response)
    assert json.loads(frames[0].split("data: ", 1)[1])["reason"] == "flag_off"


async def test_a_triage_with_the_runtime_flag_off_answers_flag_off(monkeypatch):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.delenv("WATCH_TRIAGE_ENABLED", raising=False)
    response = routes._watch_triage_invocation(_invocation("headless-abc"), current_user=object())
    frames = await _body(response)
    assert json.loads(frames[0].split("data: ", 1)[1])["reason"] == "flag_off"
