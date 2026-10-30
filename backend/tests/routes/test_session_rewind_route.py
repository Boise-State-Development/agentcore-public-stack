"""POST /sessions/{id}/rewind and GET /messages' hiding of rewound turns.

docs/specs/conversation-rewind-and-fork.md §4.2 and §5. The endpoint is one
conditional write to the session row; these pin what goes into it: the span,
the compaction rules for a cut, and the latest-turn breadcrumbs it clears.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.app_api.sessions.services import rewind_service as svc
from apis.shared.sessions.rewind import RewindState

SESSION = "11111111-2222-3333-4444-555555555555"
USER = "user-1"


def _conversation(turns):
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": [{"text": f"question {i}"}]})
        messages.append({"role": "assistant", "content": [{"text": f"answer {i}"}]})
    return messages


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "sessions-metadata")


def _run(row, stored, message_id, display_text=None):
    table = MagicMock()
    with patch.object(svc, "get_dynamodb_table", return_value=table), \
            patch.object(svc, "_get_session_by_gsi", new=AsyncMock(return_value=row)), \
            patch.object(svc, "list_stored_messages", return_value=stored), \
            patch.object(svc, "get_user_display_text", return_value=display_text), \
            patch.object(svc, "acquire_session_lease", new=AsyncMock(return_value=None)), \
            patch.object(svc, "release_session_lease", new=AsyncMock()):
        import asyncio

        result = asyncio.run(svc.rewind_session(SESSION, USER, message_id))
    return result, table


class TestRewindService:
    def test_records_a_span_and_returns_the_text_and_count(self, env):
        row = {"SK": f"S#{SESSION}", "pausedTurn": {"x": 1}, "lastTurnContinuable": True}
        stored = _conversation(3)
        stored[2]["content"][0]["text"] = "question 1\n\n[Attached files: notes.pdf]"

        result, table = _run(row, stored, f"msg-{SESSION}-2")

        assert result.next_message_index == 6
        assert result.prefill_text == "question 1"
        assert result.rewound_message_ids == [f"msg-{SESSION}-{i}" for i in range(2, 6)]
        kwargs = table.update_item.call_args.kwargs
        written = RewindState.from_dict(kwargs["ExpressionAttributeValues"][":rw"])
        assert written.spans == ((2, 6),) and written.epoch == 1
        assert kwargs["ConditionExpression"] == "attribute_not_exists(#rw)"
        removed = {kwargs["ExpressionAttributeNames"][n] for n in kwargs["UpdateExpression"].split("REMOVE ")[1].split(", ")}
        assert removed == {"pausedTurn", "lastTurnContinuable"}

    def test_prefers_the_stored_display_text(self, env):
        row = {"SK": f"S#{SESSION}"}
        result, _ = _run(row, _conversation(2), f"msg-{SESSION}-2", display_text="what I typed")

        assert result.prefill_text == "what I typed"

    def test_clamps_the_truncation_anchor_and_clears_a_parked_cut(self, env):
        row = {
            "SK": f"S#{SESSION}",
            "compaction": {"checkpoint": 0, "truncationAnchor": 6.0, "pendingCheckpoint": 4.0, "summary": None},
        }
        _, table = _run(row, _conversation(4), f"msg-{SESSION}-2")

        compaction = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":cmp"]
        assert compaction["truncationAnchor"] == 2
        assert compaction["pendingCheckpoint"] is None

    def test_a_second_rewind_is_conditional_on_the_epoch_it_read(self, env):
        row = {"SK": f"S#{SESSION}", "rewind": RewindState().with_cut(6, 8).to_dict()}
        _, table = _run(row, _conversation(4), f"msg-{SESSION}-2")

        kwargs = table.update_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == "#rw.#ep = :prev"
        assert kwargs["ExpressionAttributeValues"][":prev"] == 1

    @pytest.mark.parametrize(
        "row_extra, message_index, status",
        [
            ({"compaction": {"checkpoint": 4}}, 4, 409),  # inside the summary
            ({"compaction": {"checkpoint": 4}}, 2, 409),
            ({"rewind": RewindState().with_cut(4, 8).to_dict()}, 6, 409),  # already hidden
            ({}, 1, 400),  # an assistant message
            ({}, 99, 404),
        ],
    )
    def test_refusals(self, env, row_extra, message_index, status):
        row = {"SK": f"S#{SESSION}", **row_extra}
        with pytest.raises(svc.RewindRefused) as refused:
            _run(row, _conversation(4), f"msg-{SESSION}-{message_index}")
        assert refused.value.status == status

    def test_a_streaming_turn_is_a_409(self, env):
        from apis.shared.sessions.session_lease import SessionBusyError

        with patch.object(svc, "get_dynamodb_table", return_value=MagicMock()), \
                patch.object(svc, "acquire_session_lease", new=AsyncMock(side_effect=SessionBusyError(SESSION))):
            import asyncio

            with pytest.raises(svc.RewindRefused) as refused:
                asyncio.run(svc.rewind_session(SESSION, USER, f"msg-{SESSION}-0"))
        assert refused.value.status == 409

    def test_another_sessions_message_id_is_a_400(self, env):
        import asyncio

        with pytest.raises(svc.RewindRefused) as refused:
            asyncio.run(svc.rewind_session(SESSION, USER, "msg-other-session-2"))
        assert refused.value.status == 400


class TestGetMessagesHidesRewoundTurns:
    @pytest.mark.asyncio
    async def test_hidden_messages_are_skipped_but_ids_stay_physical(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
        stored = [MagicMock(message=m) for m in _conversation(3)]
        memory = MagicMock()
        memory.list_messages.return_value = stored
        state = RewindState().with_cut(2, 4)

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=memory), \
                patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
                patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
                patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
                patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]), \
                patch("apis.shared.sessions.metadata.get_session_rewind", new_callable=AsyncMock, return_value=state):
            from apis.shared.sessions.messages import get_messages_from_cloud

            result = await get_messages_from_cloud("s1", "u1")

        assert [m.id for m in result.messages] == ["msg-s1-0", "msg-s1-1", "msg-s1-4", "msg-s1-5"]
        assert result.next_message_index == 6

    @pytest.mark.asyncio
    async def test_flag_off_shows_everything_and_sends_no_index(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
        monkeypatch.delenv("SESSION_REWIND_ENABLED", raising=False)
        memory = MagicMock()
        memory.list_messages.return_value = [MagicMock(message=m) for m in _conversation(2)]

        with patch("apis.shared.sessions.messages.AgentCoreMemorySessionManager", return_value=memory), \
                patch("apis.shared.sessions.messages.AgentCoreMemoryConfig"), \
                patch("apis.shared.sessions.messages.AGENTCORE_MEMORY_AVAILABLE", True), \
                patch("apis.shared.sessions.metadata.get_all_message_metadata", new_callable=AsyncMock, return_value={}), \
                patch("apis.shared.sessions.metadata.get_pending_interrupts", new_callable=AsyncMock, return_value=[]):
            from apis.shared.sessions.messages import get_messages_from_cloud

            result = await get_messages_from_cloud("s1", "u1")

        assert len(result.messages) == 4
        assert result.next_message_index is None


class TestRoute:
    def test_404_while_the_flag_is_off(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apis.app_api.sessions.routes import router
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.delenv("SESSION_REWIND_ENABLED", raising=False)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id=USER)

        response = TestClient(app).post(f"/sessions/{SESSION}/rewind", json={"messageId": f"msg-{SESSION}-0"})

        assert response.status_code == 404

    def test_answers_the_result_by_alias(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apis.app_api.sessions.routes import router
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id=USER)
        result = svc.RewindResult(next_message_index=6, prefill_text="edit me", rewound_message_ids=["m"], epoch=1)

        with patch.object(svc, "rewind_session", new=AsyncMock(return_value=result)):
            response = TestClient(app).post(f"/sessions/{SESSION}/rewind", json={"messageId": f"msg-{SESSION}-2"})

        assert response.status_code == 200
        assert response.json() == {"nextMessageIndex": 6, "prefillText": "edit me", "rewoundMessageIds": ["m"], "epoch": 1}

    def test_a_refusal_keeps_its_status_and_detail(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apis.app_api.sessions.routes import router
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id=USER)

        with patch.object(svc, "rewind_session", new=AsyncMock(side_effect=svc.RewindRefused(409, "busy"))):
            response = TestClient(app).post(f"/sessions/{SESSION}/rewind", json={"messageId": f"msg-{SESSION}-2"})

        assert response.status_code == 409 and response.json()["detail"] == "busy"
