"""`session_has_messages`: an existence check priced like one.

The agent-binding rules ask "does this thread already have messages?" before
the stream opens, on an agent conversation's first turn and on every
`@`-mention. It used to be answered with `get_messages(limit=1)`, which read the
whole history plus its metadata, interrupts, UI resources and tool summaries
to return one message (~540ms on dev, growing with history), and wrote a
`SESSION` event into a session that had none. Now it is one `ListEvents`:
`maxResults=1`, no payloads, filtered server-side to events that are not the
Memory SDK's own state records.

What must hold: the same answer (any message from any agent → True; only the
SDK's state records, or nothing → False), no write, fail-open on a failed read.
"""

from typing import Any, Dict, List

import pytest

from apis.shared.sessions import messages as messages_module
from apis.shared.sessions.messages import session_has_messages


class _Client:
    def __init__(self, pages: List[Dict[str, Any]] = None, raises: Exception = None) -> None:
        self.pages = list(pages or [])
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def list_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.pages.pop(0)


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def _install(**kwargs):
        holder["client"] = _Client(**kwargs)
        monkeypatch.setattr(
            "apis.shared.aws_clients.get_client", lambda service, region_name=None: holder["client"]
        )
        return holder["client"]

    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-1")
    return _install


@pytest.mark.asyncio
async def test_asks_for_one_non_state_event_without_payloads(client):
    fake = client(pages=[{"events": [{"eventId": "e1"}]}])

    assert await session_has_messages("sess-1", "user-1") is True

    [call] = fake.calls
    assert call == {
        "memoryId": "mem-1",
        "actorId": "user-1",
        "sessionId": "sess-1",
        "maxResults": 1,
        "includePayloads": False,
        "filter": {
            "eventMetadata": [{"left": {"metadataKey": "stateType"}, "operator": "NOT_EXISTS"}]
        },
    }


@pytest.mark.asyncio
async def test_no_message_events_is_no_messages(client):
    client(pages=[{"events": []}])
    assert await session_has_messages("sess-1", "user-1") is False


@pytest.mark.asyncio
async def test_an_empty_page_with_a_token_is_not_the_answer(client):
    fake = client(pages=[{"events": [], "nextToken": "t1"}, {"events": [{"eventId": "e9"}]}])

    assert await session_has_messages("sess-1", "user-1") is True
    assert fake.calls[1]["nextToken"] == "t1"


@pytest.mark.asyncio
async def test_a_failed_read_fails_open(client):
    client(raises=RuntimeError("throttled"))
    assert await session_has_messages("sess-1", "user-1") is False


@pytest.mark.asyncio
async def test_no_memory_configured_still_raises(monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    with pytest.raises(ValueError):
        await session_has_messages("sess-1", "user-1")


@pytest.mark.asyncio
async def test_never_builds_a_session_manager(client, monkeypatch):
    """Constructing one writes a SESSION event into a session that has none."""

    def _no(*args, **kwargs):
        raise AssertionError("session_has_messages must not construct a session manager")

    monkeypatch.setattr(messages_module, "AgentCoreMemorySessionManager", _no, raising=False)
    client(pages=[{"events": []}])

    assert await session_has_messages("sess-1", "user-1") is False


def test_the_sdk_still_tags_its_state_records_with_the_key_we_filter_on():
    """The whole check rests on this: message events carry no `stateType`, the
    SDK's SESSION/AGENT records do, and user metadata cannot set it."""
    from bedrock_agentcore.memory.integrations.strands import session_manager as sdk

    assert sdk.STATE_TYPE_KEY == messages_module._SDK_STATE_METADATA_KEY
    assert sdk.STATE_TYPE_KEY in sdk.RESERVED_METADATA_KEYS
    assert {s.value for s in sdk.StateType} >= {"SESSION", "AGENT"}


@pytest.mark.asyncio
async def test_the_route_check_delegates_to_it(monkeypatch):
    import apis.inference_api.chat.routes as routes

    seen = []

    async def _fake(session_id, user_id):
        seen.append((session_id, user_id))
        return True

    monkeypatch.setattr(messages_module, "session_has_messages", _fake)

    assert await routes._session_has_messages(session_id="sess-1", user_id="user-1") is True
    assert seen == [("sess-1", "user-1")]
