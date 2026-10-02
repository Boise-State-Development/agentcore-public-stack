"""The knowledge-base search overlaps the agent build (turn-path spec §5 P3b).

An agent turn used to await its knowledge-base search in the route, before the
stream opened, and only then build the agent. The build reads neither the
retrieved chunks nor the augmented message — only the stream does, for the
citation frames and `final_message` — so the search now starts where it always
did and is awaited after the build. What must hold, most expensive first:

1. **The model sees the same bytes.** `final_message` is the augmented message
   the inline search produced, and the citations are the same — they land in
   the persisted user message, which is the cacheable prefix on every later
   turn.
2. **The search is in flight while the agent is built.** The fake search below
   cannot finish until the build has started; awaited inline, it never would.
3. **Frame order is unchanged**: `prepared` → `citation`* → `message_start`.
4. A failed search still fails open, and `KB_SEARCH_AHEAD_ENABLED=false`
   restores the inline await.

Driven through the real `/invocations` handler; only the services behind it
are stubbed.
"""

import asyncio
import json
from typing import Any, List
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat.routes import router
from apis.shared.assistants.models import Assistant
from apis.shared.assistants.rag_service import augment_prompt_with_context
from apis.shared.auth.dependencies import get_current_user_trusted

CHUNKS = [
    {"text": "Register in myBoiseState under Student Center.", "distance": 0.1,
     "metadata": {"document_id": "doc-1", "source": "register.pdf"}, "key": "k1"},
    {"text": "Add classes to your shopping cart first.", "distance": 0.2,
     "metadata": {"document_id": "doc-2", "source": "cart.pdf"}, "key": "k2"},
]
CAP = 2000
MESSAGE = "How do I register for classes?"


def _assistant() -> Assistant:
    return Assistant(
        assistantId="ast-kb",
        ownerId="user-1",
        ownerName="Owner",
        name="Registrar",
        description="d",
        instructions="Answer from the knowledge base.",
        vectorIndexId="idx",
        visibility="PRIVATE",
        createdAt="t",
        updatedAt="t",
        status="COMPLETE",
    )


def _frames(body: str, kind: str) -> List[dict]:
    prefix = f"event: {kind}\ndata: "
    return [json.loads(f[len(prefix):]) for f in body.split("\n\n") if f.startswith(prefix)]


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(router)
    user = make_user(raw_token="fake-jwt-token", user_id="user-1")
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


class _Turn:
    """One agent turn: a build that marks when it starts, a search that can be
    made to wait for it, and an agent that records what it was handed."""

    def __init__(self, *, search_waits_for_build: bool, search_fails: bool = False) -> None:
        self.build_started = asyncio.Event()
        self.search_waits_for_build = search_waits_for_build
        self.search_fails = search_fails
        self.search_saw_build = None
        self.stream_kwargs: dict = {}
        self.final_message: Any = None

    def get_agent(self, *args, **kwargs):
        self.build_started.set()
        turn = self

        class _Agent:
            # A plain object, not a MagicMock: the route reads optional
            # attributes off the agent (pending MCP App context, interrupts),
            # and a mock answers every one of them with something truthy.
            async def stream_async(self, message, **kw):
                turn.final_message = message
                turn.stream_kwargs = kw
                yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
                yield "event: done\ndata: {}\n\n"

        return _Agent()

    async def search(self, **kwargs):
        if self.search_waits_for_build:
            try:
                await asyncio.wait_for(self.build_started.wait(), timeout=2)
                self.search_saw_build = True
            except asyncio.TimeoutError:
                self.search_saw_build = False
                raise
        if self.search_fails:
            raise RuntimeError("s3vectors throttled")
        return list(CHUNKS)

    def post(self, client: TestClient, session_id: str = "preview-kb-1"):
        assistant = _assistant()
        with patch.object(routes_module, "get_agent", side_effect=self.get_agent), patch.object(
            routes_module, "is_quota_enforcement_enabled", return_value=False
        ), patch.object(routes_module, "agents_enabled", return_value=False), patch.object(
            routes_module, "_session_has_messages", AsyncMock(return_value=False)
        ), patch(
            "apis.shared.assistants.service.get_assistant_with_access_check",
            AsyncMock(return_value=(assistant, "owner")),
        ), patch(
            "apis.shared.assistants.version_resolution.resolve_invocation_agent",
            AsyncMock(return_value=(assistant, None)),
        ), patch(
            "apis.shared.assistants.service.bump_last_used_at", AsyncMock(return_value=False)
        ), patch(
            "apis.shared.sessions.metadata.get_session_metadata", AsyncMock(return_value=None)
        ), patch(
            "apis.shared.sessions.metadata.store_session_metadata", AsyncMock()
        ), patch(
            "apis.shared.assistants.rag_service.search_assistant_knowledgebase_with_formatting",
            side_effect=self.search,
        ), patch(
            "apis.shared.assistants.rag_service.resolve_context_cap", return_value=CAP
        ):
            return client.post(
                "/invocations",
                json={"session_id": session_id, "message": MESSAGE, "rag_assistant_id": "ast-kb"},
            )


EXPECTED = augment_prompt_with_context(user_message=MESSAGE, context_chunks=CHUNKS, max_context_length=CAP)


class TestSearchAheadOfTheBuild:
    def test_the_search_is_in_flight_while_the_agent_is_built(self, client, monkeypatch):
        monkeypatch.delenv("KB_SEARCH_AHEAD_ENABLED", raising=False)
        turn = _Turn(search_waits_for_build=True)

        resp = turn.post(client)

        assert resp.status_code == 200
        assert turn.search_saw_build is True
        assert turn.final_message == EXPECTED

    def test_the_model_gets_the_same_message_and_citations_either_way(self, client, monkeypatch):
        monkeypatch.delenv("KB_SEARCH_AHEAD_ENABLED", raising=False)
        ahead = _Turn(search_waits_for_build=False)
        ahead_body = ahead.post(client).text

        monkeypatch.setenv("KB_SEARCH_AHEAD_ENABLED", "false")
        inline = _Turn(search_waits_for_build=False)
        inline_body = inline.post(client).text

        assert ahead.final_message == inline.final_message == EXPECTED
        assert ahead.stream_kwargs["citations"] == inline.stream_kwargs["citations"]
        assert len(ahead.stream_kwargs["citations"]) == len(CHUNKS)
        assert ahead.stream_kwargs["original_message"] == MESSAGE
        assert _frames(ahead_body, "citation") == _frames(inline_body, "citation")

    def test_citations_go_out_after_prepared_and_before_the_answer(self, client, monkeypatch):
        monkeypatch.delenv("KB_SEARCH_AHEAD_ENABLED", raising=False)
        body = _Turn(search_waits_for_build=False).post(client).text

        prepared = body.index('"phase": "prepared"')
        first_citation = body.index("event: citation")
        message_start = body.index("event: message_start")
        assert prepared < first_citation < message_start
        assert len(_frames(body, "citation")) == len(CHUNKS)

    def test_a_failed_search_still_fails_open(self, client, monkeypatch):
        monkeypatch.delenv("KB_SEARCH_AHEAD_ENABLED", raising=False)
        turn = _Turn(search_waits_for_build=False, search_fails=True)

        body = turn.post(client).text

        assert turn.final_message == MESSAGE
        assert "event: citation" not in body
        assert turn.stream_kwargs["citations"] is None

    def test_kill_switch_awaits_the_search_before_the_build(self, client, monkeypatch):
        """Inline, a search that waits for the build can never see it — it
        times out and the turn fails open, which is what proves the order."""
        monkeypatch.setenv("KB_SEARCH_AHEAD_ENABLED", "false")
        turn = _Turn(search_waits_for_build=True)

        turn.post(client)

        assert turn.search_saw_build is False
        assert turn.final_message == MESSAGE
