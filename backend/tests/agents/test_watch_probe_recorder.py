"""``WatchProbeRecorder`` and where it is registered (docs/specs/quiet-watcher-triage.md §3.1).

The hook records every call but ``report``, judges read-only by ``runs_in_parallel``, and
fingerprints the tool's own result before any other ``AfterToolCallEvent`` hook rewrites it.
The route registers it only on a headless watch run while ``WATCH_TRIAGE_ENABLED`` is on, never
on an interactive turn, a turn without ``run_mode``, a resume or a continuation, and streams its
one frame just before ``done``.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest
from strands.hooks import AfterToolCallEvent, HookRegistry

from agents.main_agent.session.hooks.watch_probe import WatchProbeRecorder
from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION, fingerprint_result

HEADLESS = "headless-abc123"


def _result(text: str, status: str = "success") -> dict:
    return {"status": status, "toolUseId": "x", "content": [{"text": text}]}


def _event(tool_use_id: str, name: str, result: dict, tool=None, **tool_input) -> AfterToolCallEvent:
    return AfterToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=tool,
        tool_use={"toolUseId": tool_use_id, "name": name, "input": tool_input},
        invocation_state={},
        result=result,
    )


def _mcp_tool(*, read_only, destructive=None):
    annotations = SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)
    return SimpleNamespace(mcp_tool=SimpleNamespace(annotations=annotations))


class _Agent:
    """Stands in for the Strands agent: ``add_hook`` onto a real registry."""

    def __init__(self) -> None:
        self.hooks = HookRegistry()

    def add_hook(self, callback, event_type, *, order):
        self.hooks.add_callback(event_type, callback, order=order)


class TestRecording:
    def test_records_every_call_but_report(self):
        recorder = WatchProbeRecorder()
        recorder.on_after_tool_call(_event("t1", "document_read", _result("a"), q="x"))
        recorder.on_after_tool_call(_event("r1", "report", _result("Recorded."), outcome="nothing_new"))
        assert [(c.tool_use_id, c.name) for c in recorder.calls] == [("t1", "document_read")]
        assert recorder.calls[0].fingerprint == fingerprint_result(_result("a"))
        assert recorder.calls[0].input_chars == len('{"q":"x"}')

    def test_read_only_is_the_concurrency_rule(self):
        recorder = WatchProbeRecorder()
        recorder.on_after_tool_call(_event("t1", "gh___list_notifications", _result("a"), tool=_mcp_tool(read_only=True)))
        recorder.on_after_tool_call(_event("t2", "gh___delete_repo", _result("b"), tool=_mcp_tool(read_only=True, destructive=True)))
        recorder.on_after_tool_call(_event("t3", "create_artifact", _result("c")))
        recorder.on_after_tool_call(_event("t4", "dev___WebSearch", _result("d")))
        assert [c.read_only for c in recorder.calls] == [True, False, False, True]
        assert recorder.payload()["reason"] == "write_tool"

    def test_a_retried_call_keeps_its_place_and_its_last_result(self):
        recorder = WatchProbeRecorder()
        recorder.on_after_tool_call(_event("t1", "document_read", _result("first")))
        recorder.on_after_tool_call(_event("t2", "document_read", _result("other")))
        recorder.on_after_tool_call(_event("t1", "document_read", _result("retried")))
        assert [c.tool_use_id for c in recorder.calls] == ["t1", "t2"]
        assert recorder.calls[0].fingerprint == fingerprint_result(_result("retried"))

    def test_a_call_it_cannot_record_leaves_no_probe_and_never_raises(self, monkeypatch):
        from agents.main_agent.session.hooks import watch_probe as hook_module

        def boom(_result):
            raise RuntimeError("unhashable")

        monkeypatch.setattr(hook_module, "fingerprint_result", boom)
        recorder = WatchProbeRecorder()
        recorder.on_after_tool_call(_event("t1", "document_read", _result("a")))
        assert recorder.payload()["probeable"] is False

    def test_the_frame(self):
        recorder = WatchProbeRecorder()
        recorder.on_after_tool_call(_event("t1", "document_read", _result("a")))
        frame = recorder.sse_frame()
        assert frame.startswith("event: watch_probe\ndata: ") and frame.endswith("\n\n")
        payload = json.loads(frame.split("data: ", 1)[1])
        assert payload == {
            "type": "watch_probe",
            "fingerprintVersion": FINGERPRINT_VERSION,
            "probeable": True,
            "calls": [{"toolUseId": "t1", "name": "document_read", "fingerprint": fingerprint_result(_result("a"))}],
        }

    def test_no_calls(self):
        assert WatchProbeRecorder().payload()["reason"] == "no_calls"


def test_fingerprints_the_result_before_any_other_hook_rewrites_it():
    """The offloader (order 70) and the MCP App note (default order) rewrite ``event.result``;
    a T.2 replay fingerprints the tool's own result, so the baseline must be that one too."""
    agent = _Agent()

    def rewrite(event: AfterToolCallEvent) -> None:
        event.result = _result("[offloaded: see retrieve_offloaded_content]")

    agent.hooks.add_callback(AfterToolCallEvent, rewrite)
    agent.hooks.add_callback(AfterToolCallEvent, rewrite, order=70)
    recorder = WatchProbeRecorder()
    recorder.register(agent)
    # Registered after the rewriters, as the route does once the agent is built.
    agent.hooks.add_callback(AfterToolCallEvent, rewrite)

    event = _event("t1", "document_read", _result("the real result"))
    agent.hooks.invoke_callbacks(event)

    assert event.result["content"][0]["text"].startswith("[offloaded")
    assert recorder.calls[0].fingerprint == fingerprint_result(_result("the real result"))


# --- Where it is registered -----------------------------------------------------------------


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.setenv("WATCH_TRIAGE_ENABLED", "true")


def _attach(*, is_resume=False, is_continuation=False, **request):
    wrapper = SimpleNamespace(agent=_Agent())
    recorder = routes._attach_watch_probe_recorder(
        wrapper, InvocationRequest(**request), is_resume=is_resume, is_continuation=is_continuation
    )
    callbacks = wrapper.agent.hooks._registered_callbacks.get(AfterToolCallEvent, [])
    return recorder, callbacks


def test_registered_on_a_headless_watch_run(on):
    recorder, callbacks = _attach(session_id=HEADLESS, run_mode="watch")
    assert isinstance(recorder, WatchProbeRecorder)
    assert len(callbacks) == 1


def test_absent_from_a_turn_without_run_mode(on):
    """The TTFT rule: an interactive turn never carries the hook (spec §3.1)."""
    assert _attach(session_id=HEADLESS) == (None, [])
    assert _attach(session_id="sess-1") == (None, [])


def test_absent_from_an_interactive_session(on):
    assert _attach(session_id="sess-1", run_mode="watch") == (None, [])


@pytest.mark.parametrize("kind", ["resume", "continuation"])
def test_absent_from_a_resumed_or_continued_leg(on, kind):
    assert _attach(session_id=HEADLESS, run_mode="watch", **{f"is_{kind}": True}) == (None, [])


@pytest.mark.parametrize(
    "proactive,triage",
    [("true", None), ("true", ""), ("true", "false"), ("true", "yes"), (None, "true"), ("false", "true")],
)
def test_absent_while_either_flag_is_off(monkeypatch, proactive, triage):
    for name, value in (("PROACTIVE_RUNS_ENABLED", proactive), ("WATCH_TRIAGE_ENABLED", triage)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert _attach(session_id=HEADLESS, run_mode="watch") == (None, [])


def test_the_frame_goes_out_once_just_before_done():
    source = inspect.getsource(routes)
    assert "if watch_probe is not None and _is_done_frame(event):" in source
    assert routes._is_done_frame("event: done\ndata: {}\n\n") is True
    assert routes._is_done_frame('event: message_stop\ndata: {"stopReason": "end_turn"}\n\n') is False
    assert routes._is_done_frame(b"event: done\n") is False
