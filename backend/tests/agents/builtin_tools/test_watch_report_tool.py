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
    assert digest == "9fbc3b02eb9a9df38214b71900776a846e83aad36f0a4882251276f09b5c01b9"


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
