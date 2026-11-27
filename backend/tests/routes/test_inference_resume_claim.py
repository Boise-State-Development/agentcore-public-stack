"""Two answers to one pause resume it once (#2486).

A pause can be answered from more than one place: the inbox's Review dialog, the
conversation's own card, a second tab, and in slice 3 a phone. Each sends a resume to
``/invocations``, and every resume takes the session lease with ``force``, so before the
claim both ran the paused turn and the stored history got two results for one tool call
(found on dev, 2026-11-27: ``docs/specs/answer-from-inbox.md`` §7 item 4).

The Runtime can't tell a bell resume from a conversation one: the worker sends the same
request a browser does. So these drive the real route against a moto sessions-metadata
table, with only the agent faked. The fake agent's ``history`` list stands in for what the
paused turn writes to its conversation: one entry per resume that actually ran.
What the bell does with its refusal is the worker's half, pinned in
``tests/lambdas/test_scheduled_runs_inbox_resume.py`` (``already_resumed`` →
``answered_elsewhere``).
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional
from unittest.mock import MagicMock, patch

import boto3
import httpx
import pytest
from fastapi import FastAPI
from moto import mock_aws

from apis.inference_api.chat.routes import router
from apis.shared.auth.dependencies import get_current_user_trusted

REGION = "us-east-1"
TABLE = "test-sessions-metadata-resume-claim"
SESSION = "headless-0000000000000001"
INTERRUPT_ID = "v1:tool_call:tu-ask:ask_user_question"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE)
    from apis.shared.aws_clients import reset_cached_clients

    reset_cached_clients()
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"} for name in ("PK", "SK", "GSI_PK", "GSI_SK")
            ],
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
    reset_cached_clients()


@pytest.fixture
def app(make_user):
    user = make_user(raw_token="fake-jwt-token")
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_current_user_trusted] = lambda: user
    application.state.user_id = user.user_id
    return application


async def _pause(user_id: str) -> None:
    """A headless run's question pause, written through the real metadata code."""
    from apis.shared.sessions.metadata import ensure_session_metadata_exists, set_paused_turn
    from apis.shared.sessions.models import PausedTurnSnapshot

    now = datetime.now(timezone.utc)
    await ensure_session_metadata_exists(SESSION, user_id)
    await set_paused_turn(
        SESSION,
        user_id,
        PausedTurnSnapshot(
            enabled_tools=["ask_user_question"],
            model_id="us.anthropic.claude-haiku-5-5",
            provider="bedrock",
            system_prompt="You are helpful.",
            agent_type="chat",
            captured_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        ),
    )


async def _paused_turn(user_id: str) -> Optional[dict]:
    from apis.shared.sessions.metadata import _get_session_by_gsi, get_dynamodb_table

    row = await _get_session_by_gsi(SESSION, user_id, get_dynamodb_table(TABLE))
    return (row or {}).get("pausedTurn")


class _Turn:
    """The paused turn the fake agents resume. ``history`` is what each run wrote."""

    def __init__(self) -> None:
        self.history: List[Any] = []
        self.during_run: Optional[Callable[[], Any]] = None

    def agent(self, *args: Any, **kwargs: Any) -> MagicMock:
        agent = MagicMock()
        state = MagicMock()
        state.activated = True
        state.interrupts = {INTERRUPT_ID: MagicMock()}
        agent.agent = MagicMock()
        agent.agent._interrupt_state = state
        turn = self

        async def stream(*_a: Any, **_k: Any):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            if turn.during_run is not None:
                during, turn.during_run = turn.during_run, None
                await during()
            # The resumed tool call's one result, then the turn ends unpaused.
            turn.history.append({"toolResult": INTERRUPT_ID})
            state.activated = False
            yield "event: done\ndata: {}\n\n"

        agent.stream_async = stream
        return agent


def _answer(choice: str) -> dict:
    return {
        "session_id": SESSION,
        "message": "",
        "interrupt_responses": [
            {"interruptId": INTERRUPT_ID, "response": {"answers": {"Calendar": {"selected": [choice], "text": ""}}}}
        ],
    }


def _stream_error(body: str) -> Optional[dict]:
    for frame in body.split("\n\n"):
        if frame.startswith("event: stream_error"):
            return json.loads(frame.split("data: ", 1)[1])
    return None


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime")


def _patches(turn: _Turn):
    return (
        patch("apis.inference_api.chat.routes.get_agent", side_effect=turn.agent),
        patch("apis.inference_api.chat.routes.is_quota_enforcement_enabled", return_value=False),
    )


class TestOneResumePerPause:
    @pytest.mark.asyncio
    async def test_an_answer_while_another_resumes_the_pause_is_refused(self, table, app):
        """The bell's resume is running when the conversation's answer lands, or the reverse."""
        await _pause(app.state.user_id)
        turn = _Turn()
        second: dict = {}
        get_agent, quota = _patches(turn)
        with get_agent, quota:
            async with _client(app) as client:

                async def answer_elsewhere() -> None:
                    response = await client.post("/invocations", json=_answer("Home"))
                    second["body"] = response.text
                    # While the first resume runs, its claim is on the pause.
                    second["claim"] = ((await _paused_turn(app.state.user_id)) or {}).get("resumeClaim")

                turn.during_run = answer_elsewhere
                first = await client.post("/invocations", json=_answer("Work"))

        assert first.status_code == 200 and _stream_error(first.text) is None
        error = _stream_error(second["body"])
        assert error["metadata"] == {"error_kind": "resume_unavailable", "reason": "already_resumed"}
        assert error["message"].startswith("This was already answered")
        assert second["claim"] and second["claim"]["owner"]
        # One result for the one paused tool call, and the pause ended with the first resume.
        assert turn.history == [{"toolResult": INTERRUPT_ID}]
        assert await _paused_turn(app.state.user_id) is None

    @pytest.mark.asyncio
    async def test_two_answers_at_once_resume_the_pause_once(self, table, app):
        """Both preambles read the pause before either claims it: one runs, one is refused.

        DynamoDB evaluates conditional writes to one item one at a time; moto does not
        serialize them across threads, so the test supplies that guarantee for the claim's
        ``update_item`` and nothing else.
        """
        await _pause(app.state.user_id)
        turn = _Turn()
        from apis.inference_api.chat import routes
        from apis.shared.sessions import metadata

        real_load = routes.load_session_meta
        both_read = asyncio.Barrier(2)

        async def load_then_wait(session_id: str, user_id: str):
            snapshot = await real_load(session_id, user_id)
            await both_read.wait()
            return snapshot

        real_table = metadata.get_dynamodb_table
        item_lock = threading.Lock()

        class _Serialized:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def update_item(self, **kwargs: Any) -> Any:
                with item_lock:
                    return self._inner.update_item(**kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        get_agent, quota = _patches(turn)
        with get_agent, quota, patch.object(routes, "load_session_meta", load_then_wait), patch.object(
            metadata, "get_dynamodb_table", lambda name: _Serialized(real_table(name))
        ):
            async with _client(app) as client:
                bell, conversation = await asyncio.gather(
                    client.post("/invocations", json=_answer("Work")),
                    client.post("/invocations", json=_answer("Home")),
                )

        errors = [_stream_error(r.text) for r in (bell, conversation)]
        assert sorted(e["metadata"]["reason"] if e else "ran" for e in errors) == ["already_resumed", "ran"]
        assert turn.history == [{"toolResult": INTERRUPT_ID}]
        assert await _paused_turn(app.state.user_id) is None

    @pytest.mark.asyncio
    async def test_a_resume_the_runtime_refuses_gives_the_pause_back(self, table, app):
        """A wrong interrupt id claims the pause, is refused, and must not hold it."""
        await _pause(app.state.user_id)
        turn = _Turn()
        get_agent, quota = _patches(turn)
        stale = _answer("Work")
        stale["interrupt_responses"][0]["interruptId"] = "v1:tool_call:tu-old:ask_user_question"
        with get_agent, quota:
            async with _client(app) as client:
                refused = await client.post("/invocations", json=stale)
                assert ((await _paused_turn(app.state.user_id)) or {}).get("resumeClaim") is None
                answered = await client.post("/invocations", json=_answer("Home"))

        assert _stream_error(refused.text)["metadata"]["reason"] == "unknown_interrupt"
        assert _stream_error(answered.text) is None
        assert turn.history == [{"toolResult": INTERRUPT_ID}]

    @pytest.mark.asyncio
    async def test_a_refused_answer_never_takes_the_running_resumes_lease(self, table, app):
        """The loser stops before the lease: a forced acquire would orphan the winner's."""
        await _pause(app.state.user_id)
        turn = _Turn()
        owners: dict = {}
        from apis.shared.sessions.session_lease import _table as lease_table

        def lease_owner() -> Optional[str]:
            item = lease_table().get_item(
                Key={"PK": f"USER#{app.state.user_id}", "SK": f"LEASE#{SESSION}"}
            ).get("Item")
            return (item or {}).get("leaseOwner")

        get_agent, quota = _patches(turn)
        with get_agent, quota:
            async with _client(app) as client:

                async def answer_elsewhere() -> None:
                    owners["before"] = lease_owner()
                    await client.post("/invocations", json=_answer("Home"))
                    owners["after"] = lease_owner()

                turn.during_run = answer_elsewhere
                await client.post("/invocations", json=_answer("Work"))

        assert owners["before"] and owners["after"] == owners["before"]
