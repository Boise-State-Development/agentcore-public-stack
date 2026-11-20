"""One brief per workflow run (workflow-runs.md §6.3, PR-W.3).

What has to hold: without ``runId`` the brief is exactly the single handoff's (instruction, request
and frames pinned here); with one, the only thing that changes is the final user message, which
names every kept step; the brief lands on the run row and on every kept step's ``HANDOFF#`` row, so
each step's acceptance compares its card against it with ``HandoffTurn`` unchanged; and the metrics
say it was a run's brief.
"""

from __future__ import annotations

import asyncio
import copy
import json
import types
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

import apis.inference_api.chat.handoff_brief as brief_module
from apis.shared.handoff import metrics as metrics_module
from apis.shared.handoff import turn as turn_module
from apis.shared.handoff.brief import (
    BRIEF_INSTRUCTION_VERSION,
    BRIEF_REQUEST_TAG,
    BRIEF_RUN_INSTRUCTION_MAX_CHARS,
    BRIEF_RUN_INSTRUCTION_VERSION,
    BRIEF_RUN_MAX_STEPS,
    BRIEF_RUN_REQUEST_MAX_CHARS,
    BRIEF_RUN_STEP_DESCRIPTION_MAX_CHARS,
    BRIEF_RUN_STEP_NAME_MAX_CHARS,
    BRIEF_RUN_STEP_REQUEST_MAX_CHARS,
    HandoffBriefRequest,
    RunBriefStep,
    brief_done_frame,
    brief_fallback_frame,
    build_brief_instruction,
    build_run_brief_instruction,
)
from apis.shared.handoff.context import context_hash
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep, WorkflowRunStore
from apis.shared.handoff.store import HandoffStore
from apis.shared.handoff.turn import HandoffContextRequest, HandoffTurn

REGION = "us-west-2"
TABLE = "test-sessions-metadata"

SOURCE = [
    {"role": "user", "content": [{"text": "here is my grant proposal"}]},
    {"role": "assistant", "content": [{"text": "It asks for $1.2M over three years."}]},
    {"role": "user", "content": [{"text": "the solicitation caps it at $1M"}]},
    {"role": "assistant", "content": [{"text": "Then the budget needs cutting."}]},
]
SYSTEM_CONTENT = [{"text": "You are helpful."}, {"cachePoint": {"type": "default"}}]
TOOL_SPECS = [{"name": "web_search", "description": "Search", "inputSchema": {"json": {"type": "object"}}}]
SHARED = "review this proposal against the solicitation"
BRIEF = "Goal: a review of the proposal."

#: The single handoff's instruction as it stood before runs existed. Pinned verbatim: a change here
#: is a change to every single brief's request, so it needs ``BRIEF_INSTRUCTION_VERSION`` bumped.
SINGLE_INSTRUCTION = (
    '<handoff_brief_request agent="Rubric Builder" agent_description="Grades lab reports.">\n'
    "Write a brief for another of the user's agents, which will continue this work in a new conversation. "
    "It will not see this conversation, only your brief and the user's request below.\n"
    "\n"
    "Write it for that request. Cover, in this order and only where there is something to say:\n"
    "- Goal: what the user is asking for, and why, as this conversation shows it.\n"
    "- Background: what matters from this conversation for the request, stated plainly.\n"
    "- Decisions and constraints: what has been settled, and limits the user has given.\n"
    "- Files and artifacts: what is in play, by name.\n"
    "- Open questions: what is still undecided.\n"
    "\n"
    'Keep it under about 800 words. Write in plain prose with short headed sections. Answer in text '
    "only: do not call any tool. Do not address the user, and do not answer the request yourself.\n"
    "\n"
    "The request: draft a rubric for this\n"
    "</handoff_brief_request>"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_row(handoff_id: str, agent_id: str, agent_name: str, *, step_id: str, request: str = SHARED) -> Handoff:
    now = _now()
    return Handoff(
        handoff_id=handoff_id,
        user_id="alice",
        session_id="src",
        kind="mention",
        agent_id=agent_id,
        agent_name=agent_name,
        request=request,
        source_title="Grant proposal",
        excerpt_hash=context_hash("the excerpt"),
        excerpt_tokens=10,
        created_at=now,
        updated_at=now,
        run_id="r1",
        step_id=step_id,
    )


def _run(*, status: str = "open", removed: str = "") -> WorkflowRun:
    now = _now()
    steps = [
        WorkflowRunStep(step_id="s1", handoff_id="h1", agent_id="generalist", agent_name="Generalist", request=SHARED),
        WorkflowRunStep(
            step_id="s2", handoff_id="h2", agent_id="fiscal", agent_name="Fiscal outsider", request="check the budget only"
        ),
        WorkflowRunStep(step_id="s3", handoff_id="h3", agent_id="colleague", agent_name="Colleague", request=SHARED),
    ]
    steps = [step.model_copy(update={"removed": step.step_id == removed}) for step in steps]
    return WorkflowRun(
        run_id="r1",
        user_id="alice",
        session_id="src",
        status=status,
        request=SHARED,
        steps=steps,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Without runId: today's brief, byte for byte
# ---------------------------------------------------------------------------


class TestSingleBriefUnchanged:
    def test_the_single_instruction_is_pinned(self):
        assert BRIEF_INSTRUCTION_VERSION == 1
        assert build_brief_instruction("draft a rubric for this", "Rubric Builder", "Grades lab reports.") == SINGLE_INSTRUCTION

    def test_the_request_field_without_run_id_is_what_it_was(self):
        request = HandoffBriefRequest.model_validate({"handoffId": "h1"})

        assert request.run_id is None
        assert request.model_dump(by_alias=True, exclude_none=True) == {"handoffId": "h1"}

    def test_the_frames_without_run_id_carry_no_run_id(self):
        assert brief_fallback_frame("h1", "busy") == (
            'event: handoff_brief_fallback\ndata: {"type": "handoff_brief_fallback", "handoffId": "h1", "reason": "busy"}\n\n'
        )
        assert brief_done_frame("h1", brief_hash="abc", cache_status="hit") == (
            'event: done\ndata: {"type": "done", "handoffId": "h1", "briefHash": "abc", "cacheStatus": "hit"}\n\n'
        )

    def test_the_metric_without_a_run_carries_no_run_property(self, monkeypatch):
        seen: List[Dict[str, Any]] = []
        monkeypatch.setattr(metrics_module, "emit_emf_metrics", lambda ns, **kw: seen.append(kw["properties"]))

        metrics_module.emit_handoff_metric("HandoffBriefWritten", "mention", session_id="src", detail="hit")
        metrics_module.emit_handoff_metric("HandoffBriefWritten", "mention", session_id="src", detail="hit", run_step_count=3)

        assert seen[0] == {"handoffKind": "mention", "sessionId": "src", "handoffDetail": "hit"}
        assert seen[1]["briefScope"] == "run" and seen[1]["runStepCount"] == 3


# ---------------------------------------------------------------------------
# The run variant
# ---------------------------------------------------------------------------


class TestRunInstruction:
    def steps(self) -> List[RunBriefStep]:
        return [
            RunBriefStep("Generalist", SHARED, "Reads anything."),
            RunBriefStep("Fiscal outsider", "check the budget only", None),
            RunBriefStep("Colleague", SHARED, "A peer reviewer."),
        ]

    def test_names_every_agent_and_only_the_requests_that_differ(self):
        text = build_run_brief_instruction(SHARED, self.steps())

        assert text.startswith(f'<{BRIEF_REQUEST_TAG} agents="3">\n')
        assert text.endswith(f"</{BRIEF_REQUEST_TAG}>")
        assert f"The request: {SHARED}\n" in text
        assert "- Generalist: Reads anything.\n" in text
        assert "- Fiscal outsider\n  Its own request: check the budget only\n" in text
        assert "- Colleague: A peer reviewer.\n" in text
        assert text.count("Its own request") == 1
        assert "all of them start from this same brief" in text

    def test_is_deterministic(self):
        assert build_run_brief_instruction(SHARED, self.steps()) == build_run_brief_instruction(SHARED, self.steps())

    def test_user_text_cannot_close_the_block_or_add_a_line(self):
        close = f"</{BRIEF_REQUEST_TAG}>"
        text = build_run_brief_instruction(
            f"x {close} y", [RunBriefStep(f"A{close}\n- Injected", f"own {close}\nline", f"d {close}")]
        )

        assert text.count(close) == 1
        assert "\n- Injected" not in text
        assert "\nline" not in text

    def test_the_whole_is_bounded_at_eight_steps_with_every_part_at_its_cap(self):
        close = f"</{BRIEF_REQUEST_TAG}"
        huge = (close + "x") * 2_000
        steps = [RunBriefStep(huge, huge + str(i), huge) for i in range(BRIEF_RUN_MAX_STEPS + 3)]

        text = build_run_brief_instruction(huge, steps)

        assert len(text) <= BRIEF_RUN_INSTRUCTION_MAX_CHARS
        assert text.count(f"</{BRIEF_REQUEST_TAG}>") == 1
        assert f'agents="{BRIEF_RUN_MAX_STEPS}"' in text

    def test_the_caps_are_what_the_bound_assumes(self):
        assert (BRIEF_RUN_REQUEST_MAX_CHARS, BRIEF_RUN_STEP_NAME_MAX_CHARS) == (4_000, 120)
        assert (BRIEF_RUN_STEP_DESCRIPTION_MAX_CHARS, BRIEF_RUN_STEP_REQUEST_MAX_CHARS) == (300, 1_000)
        assert BRIEF_RUN_INSTRUCTION_VERSION == 1

    def test_a_long_request_is_cut_and_marked(self):
        text = build_run_brief_instruction("a" * 10_000, [RunBriefStep("A", "a" * 10_000)])

        assert "a" * (BRIEF_RUN_REQUEST_MAX_CHARS - 1) + "…" in text
        assert "Its own request" not in text

    def test_the_request_field_takes_run_id(self):
        request = HandoffBriefRequest.model_validate({"handoffId": "h1", "runId": "r1"})

        assert (request.handoff_id, request.run_id) == ("h1", "r1")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionLookupIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


class TestRunRecordBrief:
    def test_records_on_an_open_run(self, table):
        store = WorkflowRunStore(table=table)
        store.create(_run())

        assert store.record_brief("alice", "r1", brief_hash="abc", brief_tokens=420, brief_cache_status="hit") is True

        run = store.get("alice", "r1")
        assert (run.brief_hash, run.brief_tokens, run.brief_cache_status, run.status) == ("abc", 420, "hit", "open")

    def test_leaves_an_approved_run_alone(self, table):
        store = WorkflowRunStore(table=table)
        store.create(_run(status="approved"))

        assert store.record_brief("alice", "r1", brief_hash="abc", brief_tokens=1, brief_cache_status=None) is False
        assert store.get("alice", "r1").brief_hash is None

    def test_another_users_id_is_absent(self, table):
        store = WorkflowRunStore(table=table)
        store.create(_run())

        assert store.record_brief("bob", "r1", brief_hash="abc", brief_tokens=1, brief_cache_status=None) is False


# ---------------------------------------------------------------------------
# The side call
# ---------------------------------------------------------------------------


class _Coordinator:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def _store_message_metadata(self, **kwargs):
        self.calls.append(kwargs)
        return {"cacheStatus": "hit"}


class _Registry:
    def get_all_tool_specs(self):
        return copy.deepcopy(TOOL_SPECS)


def _main_agent():
    strands_agent = types.SimpleNamespace(
        messages=copy.deepcopy(SOURCE),
        system_prompt="You are helpful.",
        _system_prompt_content=copy.deepcopy(SYSTEM_CONTENT),
        tool_registry=_Registry(),
        model=object(),
        _model_state={},
        _metadata=None,
    )
    return types.SimpleNamespace(agent=strands_agent, session_manager=None, stream_coordinator=_Coordinator())


class _World:
    """The model stubbed; the stores either the moto table's or recorders."""

    def __init__(self, monkeypatch, *, table=None, chunks=(BRIEF,)) -> None:
        self.model_calls: List[Dict[str, Any]] = []
        self.recorded: List[tuple] = []
        self.metrics: List[tuple] = []
        world = self

        async def fake_stream(model, system_prompt, messages, tool_specs, **kwargs):
            world.model_calls.append(
                {"model": model, "system_prompt": system_prompt, "messages": copy.deepcopy(messages), "tool_specs": tool_specs, **kwargs}
            )
            yield {"event": {"messageStart": {"role": "assistant"}}}
            for chunk in chunks:
                yield {"data": chunk}
            final = {"role": "assistant", "content": [{"text": "".join(chunks)}]}
            yield {"stop": ("end_turn", final, {"inputTokens": 400, "outputTokens": 900, "totalTokens": 1300, "cacheReadInputTokens": 30000}, {})}

        class _Recorder:
            def __init__(self, kind: str) -> None:
                self.kind = kind

            def record_brief(self, user_id, row_id, **kwargs):
                world.recorded.append((self.kind, user_id, row_id, kwargs))
                return True

        monkeypatch.setattr(brief_module, "stream_messages", fake_stream)
        if table is None:
            monkeypatch.setattr(brief_module, "get_handoff_store", lambda: _Recorder("handoff"))
            monkeypatch.setattr(brief_module, "get_workflow_run_store", lambda: _Recorder("run"))
        else:
            monkeypatch.setattr(brief_module, "get_handoff_store", lambda: HandoffStore(table=table))
            monkeypatch.setattr(brief_module, "get_workflow_run_store", lambda: WorkflowRunStore(table=table))
        monkeypatch.setattr(
            brief_module,
            "emit_handoff_metric",
            lambda name, kind, **kw: world.metrics.append((name, kw.get("detail"), kw.get("run_step_count"))),
        )

    def run(self, main_agent, **overrides) -> List[Dict[str, Any]]:
        async def build():
            return main_agent

        async def release():
            return None

        async def go():
            kwargs = dict(
                handoff=_step_row("h1", "generalist", "Generalist", step_id="s1"),
                user_id="alice",
                session_id="src",
                session_row=None,
                build_agent=build,
                release_lease=release,
                lease=None,
            )
            for name, value in overrides.items():
                if name in ("agent_description", "run_descriptions") and value is not None:
                    value = asyncio.ensure_future(value)
                kwargs[name] = value
            frames = []
            async for frame in brief_module.stream_handoff_brief(**kwargs):
                event = frame.split("\n", 1)[0].removeprefix("event: ")
                frames.append({"event": event, "data": json.loads(frame.split("data: ", 1)[1])})
            return frames

        return asyncio.run(go())


async def _value(value):
    return value


class TestRunBriefStream:
    def test_only_the_final_message_differs_from_a_single_brief(self, monkeypatch):
        world = _World(monkeypatch)
        single_agent, run_agent = _main_agent(), _main_agent()

        world.run(single_agent)
        world.run(run_agent, run=_run(), run_descriptions=_value(["Reads anything.", None, "A peer reviewer."]))

        single, run = world.model_calls
        assert run["messages"][:-1] == single["messages"][:-1] == SOURCE
        for key in ("system_prompt", "system_prompt_content", "tool_specs", "tool_choice", "model_state"):
            assert run[key] == single[key], key
        assert run["model"] is run_agent.agent.model
        assert run["messages"][-1] == {
            "role": "user",
            "content": [
                {
                    "text": build_run_brief_instruction(
                        SHARED,
                        [
                            RunBriefStep("Generalist", SHARED, "Reads anything."),
                            RunBriefStep("Fiscal outsider", "check the budget only", None),
                            RunBriefStep("Colleague", SHARED, "A peer reviewer."),
                        ],
                    )
                }
            ],
        }
        assert run_agent.agent.messages == SOURCE

    def test_records_on_the_run_and_every_kept_step(self, monkeypatch):
        world = _World(monkeypatch)

        frames = world.run(_main_agent(), run=_run(removed="s3"), run_descriptions=_value([None, None]))

        facts = {"brief_hash": context_hash(BRIEF), "brief_tokens": 900, "brief_cache_status": "hit"}
        assert sorted(world.recorded, key=lambda r: (r[0], r[2])) == [
            ("handoff", "alice", "h1", facts),
            ("handoff", "alice", "h2", facts),
            ("run", "alice", "r1", facts),
        ]
        assert frames[-1]["data"] == {
            "type": "done",
            "handoffId": "h1",
            "runId": "r1",
            "briefHash": context_hash(BRIEF),
            "cacheStatus": "hit",
        }
        assert ("HandoffBriefWritten", "hit", 2) in world.metrics

    def test_the_cost_row_names_the_run_instruction_and_its_version(self, monkeypatch):
        world = _World(monkeypatch)
        single_agent, run_agent = _main_agent(), _main_agent()

        world.run(single_agent)
        world.run(run_agent, run=_run(), run_descriptions=_value([None, None, None]))

        [single_row], [run_row] = single_agent.stream_coordinator.calls, run_agent.stream_coordinator.calls
        assert single_row["call_kind"] == run_row["call_kind"] == "handoff_brief"
        assert single_row["message_id"] is None and run_row["message_id"] is None
        assert single_row["call_attributes"] == {"briefInstruction": "handoff", "briefInstructionVersion": BRIEF_INSTRUCTION_VERSION}
        assert run_row["call_attributes"] == {
            "briefInstruction": "run",
            "briefInstructionVersion": BRIEF_RUN_INSTRUCTION_VERSION,
            "runStepCount": 3,
        }
        assert single_row["prefix_fingerprint"]["toolConfigHash"] == run_row["prefix_fingerprint"]["toolConfigHash"]
        assert single_row["prefix_fingerprint"]["systemPromptHash"] == run_row["prefix_fingerprint"]["systemPromptHash"]

    def test_a_failed_description_read_still_briefs(self, monkeypatch):
        world = _World(monkeypatch)

        async def broken():
            raise RuntimeError("assistants table unavailable")

        frames = world.run(_main_agent(), run=_run(), run_descriptions=broken())

        assert frames[-1]["data"]["briefHash"] == context_hash(BRIEF)
        assert "- Generalist\n" in world.model_calls[0]["messages"][-1]["content"][0]["text"]

    def test_a_fallback_says_it_was_a_run(self, monkeypatch):
        world = _World(monkeypatch)
        agent = _main_agent()
        agent.agent.messages = copy.deepcopy(SOURCE[:2])

        frames = world.run(agent, run=_run(), run_descriptions=_value([None, None, None]))

        assert frames[0]["data"] == {"type": "handoff_brief_fallback", "handoffId": "h1", "reason": "short_source", "runId": "r1"}
        assert frames[-1]["data"] == {"type": "done", "handoffId": "h1", "runId": "r1"}
        assert ("HandoffBriefFallback", "short_source", 3) in world.metrics
        assert world.recorded == []


# ---------------------------------------------------------------------------
# Acceptance of a step against the run's brief, HandoffTurn unchanged
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_rows(table):
    handoffs = HandoffStore(table=table)
    for row in (
        _step_row("h1", "generalist", "Generalist", step_id="s1"),
        _step_row("h2", "fiscal", "Fiscal outsider", step_id="s2", request="check the budget only"),
        _step_row("h3", "colleague", "Colleague", step_id="s3"),
    ):
        handoffs.create(row)
    WorkflowRunStore(table=table).create(_run())
    return handoffs


@pytest.fixture()
def meta(monkeypatch):
    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "set_session_spawned_from", lambda *a: True)
    monkeypatch.setattr(metadata, "settle_session_spawned_from", lambda *a: None)


def _accept(store: HandoffStore, handoff_id: str, agent_id: str, text: str, target: str) -> None:
    async def go():
        turn = HandoffTurn.for_turn(
            user_id="alice",
            session_id=target,
            agent_id=agent_id,
            handoff_context=HandoffContextRequest(handoff_id=handoff_id, text=text),
            session_row=None,
            is_resume=False,
            is_continuation=False,
            store=store,
        )
        assert turn is not None
        turn.start()
        await turn.context_block()
        pending = list(turn_module._background)
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(go())


class TestStepAcceptance:
    def test_each_steps_card_is_compared_with_the_runs_brief(self, table, run_rows, meta, monkeypatch):
        world = _World(monkeypatch, table=table)

        world.run(_main_agent(), run=_run(), run_descriptions=_value([None, None, None]))

        assert WorkflowRunStore(table=table).get("alice", "r1").brief_hash == context_hash(BRIEF)
        for handoff_id in ("h1", "h2", "h3"):
            row = run_rows.get("alice", handoff_id)
            assert (row.brief_hash, row.brief_tokens, row.brief_cache_status) == (context_hash(BRIEF), 900, "hit")

        _accept(run_rows, "h2", "fiscal", BRIEF, "tgt-2")
        _accept(run_rows, "h3", "colleague", BRIEF + " Also check the match.", "tgt-3")

        assert run_rows.get("alice", "h2").context_edited is False
        assert run_rows.get("alice", "h3").context_edited is True
