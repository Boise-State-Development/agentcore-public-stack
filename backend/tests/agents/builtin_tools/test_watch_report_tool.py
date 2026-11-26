"""The quiet watcher's ``report`` tool (quiet-watcher spec §3.3): the pinned spec, the gate, its place."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.builtin_tools.watch_report_tool import RECORDED, make_watch_report_tool
from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.auth.models import User

HEADLESS = "headless-abc123"


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")


def _names(**request) -> list:
    return [t.tool_name for t in routes._build_watch_report_tools(InvocationRequest(**request))]


def test_the_spec_is_pinned():
    """The spec sits in the cached ``toolConfig`` of every watch run, so a change is one
    deliberate prefix rewrite per deployment. If a change is intended, re-measure its tokens,
    state the growth in the PR, and update the digest."""
    spec = make_watch_report_tool().tool_spec
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert spec["name"] == "report"
    assert digest == "bd5dee301a4175b9b9c5bc6b3c31c37394152cb126ede342fdae6f054eac471c"


def test_every_instance_has_the_same_spec():
    assert make_watch_report_tool().tool_spec == make_watch_report_tool().tool_spec


def test_the_body_only_acknowledges():
    tool = make_watch_report_tool()
    assert tool._tool_func(outcome="nothing_new") == RECORDED
    assert tool._tool_func(
        outcome="notify",
        items=[{"key": "k", "title": "t"}],
        state={"cursors": {"a": "b"}},
    ) == RECORDED


def test_built_on_a_headless_watch_run(on):
    assert _names(session_id=HEADLESS, run_mode="watch") == ["report"]


def test_not_without_run_mode(on):
    assert _names(session_id=HEADLESS) == []


def test_not_on_an_interactive_session(on):
    assert _names(session_id="sess-1", run_mode="watch") == []


@pytest.mark.parametrize("value", [None, "", "false", "yes"])
def test_not_while_the_runtime_flag_is_off(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROACTIVE_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", value)
    assert _names(session_id=HEADLESS, run_mode="watch") == []


def test_run_mode_accepts_only_watch():
    assert InvocationRequest(session_id="s").run_mode is None
    with pytest.raises(ValueError):
        InvocationRequest(session_id="s", run_mode="standard")


def test_the_tool_is_in_no_catalog_set():
    from apis.shared.tools.injected import INJECTED_TOOL_IDS, KEY_DESCRIBED_INJECTED_TOOL_IDS

    assert "report" not in INJECTED_TOOL_IDS
    assert "report" not in KEY_DESCRIBED_INJECTED_TOOL_IDS


async def _turn_tools(input_data):
    user = User(email="user@example.com", user_id="user-1", name="User One", roles=[])

    async def _has_document(*_args, **_kwargs) -> bool:
        return True

    with patch.object(routes, "_document_tools_gate", side_effect=_has_document):
        return await routes._build_turn_tools(
            input_data,
            user,
            user.user_id,
            ["create_artifact"],
            project_memory=None,
            turn_has_document=True,
        )


@pytest.mark.asyncio
async def test_appended_after_every_other_family_and_never_cached(on, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    plain = await _turn_tools(SimpleNamespace(rag_assistant_id=None, session_id=HEADLESS))
    watch = await _turn_tools(SimpleNamespace(rag_assistant_id=None, session_id=HEADLESS, run_mode="watch"))

    plain_names = [t.tool_name for t in plain.extra_tools]
    watch_names = [t.tool_name for t in watch.extra_tools]
    assert "document_read" in plain_names
    assert watch_names == plain_names + ["report"]
    assert plain.extra_tools_key_described is True
    assert watch.extra_tools_key_described is False


# --- Side calls a watch run does not make (dev readout, 2026-11-25) --------------
# Measured on dev: every watch run also paid a Nova Micro title and a tool-batch
# summary, for a conversation that is deleted when the run is quiet.


def test_a_watch_run_is_recognised_exactly_where_report_is_built(on):
    assert routes._is_watch_run(InvocationRequest(session_id=HEADLESS, run_mode="watch")) is True
    assert routes._is_watch_run(InvocationRequest(session_id=HEADLESS)) is False
    assert routes._is_watch_run(InvocationRequest(session_id="sess-1", run_mode="watch")) is False


def test_the_route_skips_the_title_and_tool_summaries_on_a_watch_run():
    import inspect

    source = inspect.getsource(routes)
    assert "if is_new_session and input_data.message and not watch_run:" in source
    assert "turn_tool_summaries=not watch_run," in source


def test_tool_batches_are_drained_but_not_summarized_on_a_watch_run(monkeypatch):
    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    monkeypatch.setenv("TOOL_SUMMARIES_ENABLED", "true")
    drained = []

    class _Hook:
        def drain_batches(self):
            drained.append(True)
            return [{"batchId": "b1", "tools": []}]

    wrapper = SimpleNamespace(agent_status_hook=_Hook())
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    tasks: list = []

    coordinator._spawn_tool_summary_tasks(wrapper, "headless-1", "user-1", tasks, summarize=False)

    assert drained == [True]
    assert tasks == []


# ── A paused watch run's resume (answer-from-inbox §3.7) ────────────────────


def _snapshot(**fields):
    from apis.shared.sessions.models import PausedTurnSnapshot

    return PausedTurnSnapshot(captured_at="2026-11-26T00:00:00Z", expires_at="2026-12-03T00:00:00Z", **fields)


def _digest(tools) -> str:
    specs = [t.tool_spec for t in tools or []]
    return hashlib.sha256(json.dumps(specs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_a_watch_runs_agent_records_its_run_mode(on):
    agent = SimpleNamespace(_construction_snapshot={})
    routes._record_run_mode_on(agent, InvocationRequest(session_id=HEADLESS, run_mode="watch"))
    assert agent._construction_snapshot == {"run_mode": "watch"}

    other = SimpleNamespace(_construction_snapshot={})
    routes._record_run_mode_on(other, InvocationRequest(session_id=HEADLESS))
    routes._record_run_mode_on(other, InvocationRequest(session_id="web-session", run_mode="watch"))
    assert other._construction_snapshot == {}


def test_the_snapshot_round_trips_run_mode():
    from apis.shared.sessions.models import PausedTurnSnapshot

    dumped = _snapshot(run_mode="watch").model_dump(by_alias=True, exclude_none=True)
    assert dumped["runMode"] == "watch"
    assert PausedTurnSnapshot.model_validate(dumped).run_mode == "watch"
    assert "runMode" not in _snapshot().model_dump(by_alias=True, exclude_none=True)


def test_a_resume_request_takes_its_run_mode_from_the_snapshot(on):
    """No resume sends ``run_mode``; the snapshot's builds ``report`` in its slot. The full
    tool parity (every family, then ``report``) is pinned in ``test_resume_tool_parity.py``."""
    resumed = routes._resume_request(InvocationRequest(session_id=HEADLESS), _snapshot(run_mode="watch"))
    paused = routes._build_watch_report_tools(InvocationRequest(session_id=HEADLESS, run_mode="watch"))
    assert _digest(routes._build_watch_report_tools(resumed)) == _digest(paused)


def test_no_report_on_any_other_resume(on, monkeypatch):
    def _report(snapshot, session_id):
        return routes._build_watch_report_tools(routes._resume_request(InvocationRequest(session_id=session_id), snapshot))

    assert _report(_snapshot(), HEADLESS) == []
    assert _report(_snapshot(run_mode="watch"), "web-session") == []
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "false")
    assert _report(_snapshot(run_mode="watch"), HEADLESS) == []


@pytest.mark.asyncio
async def test_the_coordinator_persists_run_mode():
    from unittest.mock import AsyncMock

    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    for construction, expected in (({"enabled_tools": [], "run_mode": "watch"}, "watch"), ({"enabled_tools": []}, None)):
        wrapper = SimpleNamespace(_construction_snapshot=construction)
        with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
            await StreamCoordinator()._persist_paused_turn_snapshot(agent, HEADLESS, "user-1", wrapper)
        assert write.await_args.args[2].run_mode == expected
