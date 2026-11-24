"""A saved workflow step's Agent version on the turn path (workflow-designer §23.3, PR-D.8b).

The rule itself (``version_pins``) and ``resolve_invocation_agent(…, pinned_version=)`` are tested in
``tests/shared/test_agent_version_pins.py``; acceptance's version check in
``tests/apis/shared/handoff/test_handoff_turn.py``; the route's refusal before the model call in
``tests/routes/test_inference_handoff.py``. This pins what the turn decides:

1. Which pin a turn resolves with: the step's first turn takes the request's, every later turn the
   conversation's ``pinnedAgentVersion`` (the request ignored), and nothing while the flag is off.
2. That ``resolve_turn_agent`` serves the pinned snapshot to everyone, and answers a refused pin
   (a takedown between turns) in the conversation.
3. That the agent cache key gains nothing: an unpinned turn keys byte-identically with the flag on
   and off, pinned v3 and current v5 differ only through the system prompt, and a paused pinned
   turn resumes onto the agent it paused (§23.1, agent-version-snapshots §4.2).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat import service
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.routes import ResolvedTurnAgent, compose_turn_system_prompt, resolve_turn_agent
from apis.shared.assistants.models import AgentVersion, Assistant
from apis.shared.assistants.version_pins import pin_refusal_message
from apis.shared.handoff.turn import HandoffContextRequest
from apis.shared.handoff.version_pin import meta_pinned_version, turn_pinned_version

AGENT_ID = "ast-pinned01"
OWNER = "user-author"
STARTER = "user-starter"
CREATED = "2026-07-01T00:00:00Z"
DRAFT = "The author's unreviewed draft."
STAMP = "2026-11-01T00:00:00Z"


@pytest.fixture()
def pinning_on(monkeypatch):
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", "true")
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.delenv("WORKFLOWS_ENABLED", raising=False)


@pytest.fixture()
def pinning_off(monkeypatch):
    monkeypatch.delenv("AGENT_VERSION_PINNING_ENABLED", raising=False)
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")


def step_row(pin: Optional[int] = 3, *, bound: Optional[str] = AGENT_ID) -> Dict[str, Any]:
    row: Dict[str, Any] = {"spawnedFrom": {"sessionId": "src", "handoffId": "h1", "runId": "r1"}}
    if bound:
        row["preferences"] = {"assistantId": bound}
    if pin is not None:
        row["pinnedAgentVersion"] = pin
    return row


def first_turn(version: Optional[int]):
    """A turn that takes a handoff, as ``HandoffTurn`` exposes it to ``turn_pinned_version``."""
    request = HandoffContextRequest(handoff_id="h1", agentVersion=version)
    from apis.shared.feature_flags import agent_version_pinning_enabled

    pinning = agent_version_pinning_enabled()
    return SimpleNamespace(
        request=request, requested_version=request.agent_version if pinning else None
    ), request


# ── 1. which pin a turn resolves with ──────────────────────────────────────────────


class TestTurnPinnedVersion:
    def pin(self, **kwargs: Any) -> Optional[int]:
        defaults: Dict[str, Any] = dict(
            handoff_turn=None, handoff_context=None, session_row=None, agent_id=AGENT_ID, is_agent_mention=False
        )
        defaults.update(kwargs)
        return turn_pinned_version(**defaults)

    def test_the_first_turn_takes_the_requests_version(self, pinning_on):
        turn, request = first_turn(3)
        assert self.pin(handoff_turn=turn, handoff_context=request) == 3

    def test_a_later_turn_takes_the_conversations_pin_and_ignores_the_request(self, pinning_on, caplog):
        request = HandoffContextRequest(handoff_id="h1", agentVersion=5)
        assert self.pin(handoff_context=request, session_row=step_row(3)) == 3
        assert "pinned to version 3" in caplog.text

    def test_the_conversations_pin_survives_the_handoff_settling(self, pinning_on):
        row = step_row(3)
        row["spawnedFrom"]["settled"] = True
        assert self.pin(session_row=row) == 3

    def test_an_unpinned_conversation_runs_unpinned(self, pinning_on):
        assert self.pin(session_row=step_row(None)) is None
        assert self.pin() is None

    def test_a_mention_of_another_agent_in_a_step_conversation_runs_unpinned(self, pinning_on):
        assert self.pin(session_row=step_row(3), agent_id="ast-other", is_agent_mention=True) is None
        assert self.pin(session_row=step_row(3, bound=None), agent_id="ast-other", is_agent_mention=True) is None

    def test_a_request_version_without_a_handoff_is_ignored(self, pinning_on, caplog):
        request = HandoffContextRequest(handoff_id="h1", agentVersion=3)
        assert self.pin(handoff_context=request) is None
        assert "takes no handoff" in caplog.text

    def test_the_flag_off_ignores_both(self, pinning_off):
        turn, request = first_turn(3)
        assert self.pin(handoff_turn=turn, handoff_context=request) is None
        assert self.pin(session_row=step_row(3)) is None

    @pytest.mark.parametrize("raw, expected", [(3, 3), (3.0, 3), (0, None), (-1, None), (3.5, None), ("3", None), (True, None), (None, None)])
    def test_only_a_positive_whole_number_reads_as_a_pin(self, raw, expected):
        assert meta_pinned_version({"pinnedAgentVersion": raw}) == expected


# ── 2. resolve_turn_agent with the pin ─────────────────────────────────────────────


def live_agent(published: Optional[int], *, instructions: str = DRAFT) -> Assistant:
    return Assistant(
        **{
            "assistantId": AGENT_ID,
            "ownerId": OWNER,
            "ownerName": "An Author",
            "name": "Reviewer",
            "description": "Reviews a proposal",
            "instructions": instructions,
            "vectorIndexId": "idx",
            "visibility": "PUBLIC",
            "createdAt": CREATED,
            "updatedAt": CREATED,
            "status": "COMPLETE",
            "listing": {
                "state": "published" if published else "taken_down",
                "category": "Research",
                "publisherId": "pub-research",
                "publishedVersion": published,
            },
        }
    )


#: v3 superseded but published (stamped); v4 a rejected submission (never stamped); v5 current.
VERSIONS = {
    n: AgentVersion(
        agent_id=AGENT_ID,
        version=n,
        name="Reviewer",
        description="d",
        instructions=f"Instructions as of v{n}.",
        published_at=None if n == 4 else STAMP,
    )
    for n in (3, 4, 5)
}


def request(**overrides: Any) -> InvocationRequest:
    body = {"session_id": "step-1", "message": "review this", "rag_assistant_id": AGENT_ID}
    body.update(overrides)
    return InvocationRequest(**body)


async def resolve(live: Assistant, user_id: str, pin: Optional[int]):
    async def get_version(agent_id: str, number: int):
        return VERSIONS.get(number)

    with patch(
        "apis.shared.assistants.service.get_assistant_with_access_check", AsyncMock(return_value=(live, "viewer"))
    ), patch("apis.shared.assistants.service.mark_share_as_interacted", AsyncMock()), patch(
        "apis.shared.assistants.service.bump_last_used_at", AsyncMock(return_value=False)
    ), patch("apis.shared.assistants.version_resolution.get_version", side_effect=get_version), patch.object(
        routes_module, "agents_enabled", return_value=False
    ), patch.object(routes_module, "assistant_kb_full_context_enabled", return_value=False):
        user = SimpleNamespace(email=f"{user_id}@example.edu", user_id=user_id, roles=["User"])
        return await resolve_turn_agent(request(), user, user_id, is_continuation=False, pinned_version=pin)


async def body_of(response: StreamingResponse) -> str:
    chunks = [chunk async for chunk in response.body_iterator]
    return "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)


@pytest.mark.asyncio
async def test_the_owner_runs_the_pinned_version_not_their_draft(pinning_on):
    resolved = await resolve(live_agent(5), OWNER, 3)

    assert isinstance(resolved, ResolvedTurnAgent)
    assert (resolved.assistant.instructions, resolved.resolved_version) == ("Instructions as of v3.", 3)


@pytest.mark.asyncio
async def test_without_a_pin_the_turn_resolves_exactly_as_before(pinning_on):
    as_owner = await resolve(live_agent(5), OWNER, None)
    as_starter = await resolve(live_agent(5), STARTER, None)

    assert (as_owner.assistant.instructions, as_owner.resolved_version) == (DRAFT, None)
    assert (as_starter.assistant.instructions, as_starter.resolved_version) == ("Instructions as of v5.", 5)


@pytest.mark.asyncio
async def test_a_takedown_between_turns_blocks_the_next_turn(pinning_on):
    """Turn 1 runs v3 from the conversation's pin; a takedown clears the pointer; turn 2 is refused."""
    pin = turn_pinned_version(
        handoff_turn=None, handoff_context=None, session_row=step_row(3), agent_id=AGENT_ID, is_agent_mention=False
    )
    before = await resolve(live_agent(5), STARTER, pin)
    assert before.assistant.instructions == "Instructions as of v3."

    after = await resolve(live_agent(None), STARTER, pin)

    assert isinstance(after, StreamingResponse)
    text = await body_of(after)
    assert pin_refusal_message("unpublished", 3, None) in text
    # Never a fall forward to the author's draft.
    assert DRAFT not in text


@pytest.mark.asyncio
async def test_a_version_never_published_is_refused_in_the_conversation(pinning_on):
    refused = await resolve(live_agent(5), STARTER, 4)

    assert isinstance(refused, StreamingResponse)
    assert pin_refusal_message("never_published", 4, 5) in await body_of(refused)


# ── 3. the agent cache key gains nothing ───────────────────────────────────────────


def cache_key(system_prompt: Optional[str]) -> tuple:
    """The turn's key with every other element held still: only the prompt can move it."""
    return service._create_cache_key(
        session_id="step-1",
        user_id=STARTER,
        enabled_tools=["web_search", "document_read"],
        model_id="us.anthropic.claude-sonnet-4-6",
        inference_params={"temperature": 0.2},
        system_prompt=system_prompt,
        caching_enabled=True,
        provider="bedrock",
        freshness_hash="fresh",
        agent_type="chat",
        skills_hash="skills",
        assistant_id=AGENT_ID,
    )


async def turn_key(row: Optional[Dict[str, Any]], user_id: str = STARTER) -> tuple:
    pin = turn_pinned_version(
        handoff_turn=None, handoff_context=None, session_row=row, agent_id=AGENT_ID, is_agent_mention=False
    )
    resolved = await resolve(live_agent(5), user_id, pin)
    prompt = compose_turn_system_prompt(
        request(), resolved.assistant, system_prompt=None, project_id=None, personal_instructions=None
    )
    return cache_key(prompt)


@pytest.mark.asyncio
async def test_an_unpinned_turn_keys_byte_identically_with_the_flag_on_and_off(monkeypatch):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    keys = {}
    for flag in ("true", ""):
        monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", flag)
        keys[flag] = (await turn_key(None), await turn_key(None, OWNER), await turn_key(step_row(None)))

    assert keys["true"] == keys[""]
    assert repr(keys["true"]).encode() == repr(keys[""]).encode()


@pytest.mark.asyncio
async def test_pinned_v3_and_current_v5_differ_only_through_the_system_prompt(pinning_on):
    pinned = await turn_key(step_row(3))
    current = await turn_key(step_row(None))

    assert pinned != current
    prompt_slot = 5  # ``_create_cache_key``'s prompt hash
    assert [i for i, (a, b) in enumerate(zip(pinned, current)) if a != b] == [prompt_slot]
    # Two runs pinned to v3 share one entry (and one cached tools-plus-system prefix).
    assert await turn_key(step_row(3)) == pinned


@pytest.fixture()
def fake_build():
    service.clear_agent_cache()

    def build(**kwargs):
        inner = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=False))
        wrapper = MagicMock(spec=["agent", "_construction_snapshot"])
        wrapper.agent = inner
        wrapper._construction_snapshot = {"system_prompt": kwargs.get("system_prompt")}
        return wrapper

    with patch.object(service, "create_agent", side_effect=build) as created, patch(
        "apis.shared.tools.freshness.get_freshness_hash", new=AsyncMock(return_value="fresh")
    ), patch("apis.shared.tools.freshness.get_disabled_tool_ids", new=AsyncMock(return_value=frozenset())):
        yield created
    service.clear_agent_cache()


@pytest.mark.asyncio
async def test_a_paused_pinned_turn_resumes_onto_the_agent_it_paused(pinning_on, fake_build):
    """The snapshot replays the pinned turn's prompt, and a version number was never a key element."""
    resolved = await resolve(live_agent(5), STARTER, 3)
    prompt = compose_turn_system_prompt(
        request(), resolved.assistant, system_prompt=None, project_id=None, personal_instructions=None
    )

    paused = await service.get_agent(session_id="step-1", user_id=STARTER, system_prompt=prompt, is_resume=False)
    paused.agent._interrupt_state.activated = True  # a tool approval is waiting

    resumed = await service.get_agent(
        session_id="step-1",
        user_id=STARTER,
        system_prompt=paused._construction_snapshot["system_prompt"],
        is_resume=True,
    )

    assert resumed is paused
    assert fake_build.call_count == 1


def test_a_resume_resolves_nothing_even_in_a_pinned_conversation(pinning_on, make_user):
    """A resume replays its ``PausedTurnSnapshot``: neither the pin nor the Agent is looked up again."""
    from datetime import datetime, timedelta, timezone

    from apis.shared.auth.dependencies import get_current_user_trusted
    from apis.shared.sessions.metadata import SessionMetaSnapshot
    from apis.shared.sessions.models import PausedTurnSnapshot

    interrupt_id = "int-approve"
    now = datetime.now(timezone.utc)
    snapshot = PausedTurnSnapshot(
        enabled_tools=["send_email"],
        model_id="us.anthropic.claude-sonnet-4-6",
        provider="bedrock",
        system_prompt="Instructions as of v3.",
        agent_type="chat",
        captured_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=10)).isoformat(),
    )
    agent = MagicMock()
    agent.agent = MagicMock()
    agent.agent._interrupt_state = MagicMock(activated=True, interrupts={interrupt_id: MagicMock()})

    async def stream(*args, **kwargs):
        yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = stream

    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_current_user_trusted] = lambda: make_user(raw_token="t", user_id=STARTER)
    resolve_spy = AsyncMock()
    pin_spy = MagicMock(return_value=3)

    with patch.object(routes_module, "get_agent", return_value=agent) as get_agent, patch.object(
        routes_module, "is_quota_enforcement_enabled", return_value=False
    ), patch.object(routes_module, "resolve_turn_agent", resolve_spy), patch.object(
        routes_module, "turn_pinned_version", pin_spy
    ), patch.object(
        routes_module,
        "load_session_meta",
        AsyncMock(return_value=SessionMetaSnapshot(row=step_row(3), owned_by_other=False)),
    ), patch("apis.shared.sessions.metadata.get_paused_turn", AsyncMock(return_value=snapshot)):
        response = TestClient(app).post(
            "/invocations",
            json={
                "session_id": "step-1",
                "message": "",
                "rag_assistant_id": AGENT_ID,
                "interrupt_responses": [{"interruptId": interrupt_id, "response": {"approved": True}}],
            },
        )
        body = response.text

    assert response.status_code == 200 and "event: done" in body
    assert resolve_spy.await_count == 0 and pin_spy.call_count == 0
    assert get_agent.call_args.kwargs["system_prompt"] == "Instructions as of v3."
