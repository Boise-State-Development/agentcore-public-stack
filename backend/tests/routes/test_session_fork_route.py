"""POST /sessions/{id}/fork (rewind/fork spec §7).

The fork copies the parent's visible history before the cut verbatim, never
re-extracting it into long-term memory, and records where it came from so the
Runtime can seed its first turn. These pin what gets copied and written.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.app_api.sessions.services import fork_service as svc
from apis.app_api.sessions.services.rewind_service import RewindRefused
from apis.shared.sessions.fork import ForkOrigin
from apis.shared.sessions.rewind import RewindState

PARENT = "11111111-2222-3333-4444-555555555555"
USER = "user-1"


def _conversation(turns):
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": [{"text": f"question {i}"}]})
        messages.append({"role": "assistant", "content": [{"text": f"answer {i}"}]})
    return messages


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SESSION_FORK_ENABLED", "true")
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "sessions-metadata")


def _run(row, stored, message_id, metadata_index=None):
    table = MagicMock()
    copies = []
    parent = SimpleNamespace(title="Budget questions", preferences=None)
    with patch.object(svc, "get_dynamodb_table", return_value=table), \
            patch.object(svc, "_get_session_by_gsi", new=AsyncMock(return_value=row)), \
            patch.object(svc, "get_session_metadata", new=AsyncMock(return_value=parent)), \
            patch.object(svc, "list_stored_messages", return_value=stored), \
            patch.object(svc, "_copy_messages", side_effect=lambda sid, uid, msgs: copies.append((sid, uid, msgs))), \
            patch.object(svc, "store_session_metadata", new=AsyncMock()) as store, \
            patch.object(svc, "get_all_message_metadata", new=AsyncMock(return_value=metadata_index or {})), \
            patch.object(svc, "store_user_display_text", new=AsyncMock()) as display, \
            patch.object(svc, "_archive"), \
            patch("apis.app_api.sessions.services.rewind_service.get_user_display_text", return_value=None):
        result = asyncio.run(svc.fork_session(PARENT, USER, message_id))
    return result, table, copies, store, display


class TestForkService:
    def test_copies_the_visible_history_before_the_cut_verbatim(self, env):
        stored = _conversation(3)
        result, table, copies, store, _ = _run({"SK": f"S#{PARENT}"}, stored, f"msg-{PARENT}-4")

        assert result.title == "Budget questions (fork)"
        assert result.prefill_text == "question 2"
        assert result.root_session_id == PARENT
        assert len(copies) == 1
        child_id, user_id, messages = copies[0]
        assert child_id == result.session_id and user_id == USER
        assert messages == stored[:4]
        written = table.update_item.call_args.kwargs
        assert written["Key"] == {"PK": f"USER#{USER}", "SK": f"S#{child_id}"}
        origin = ForkOrigin.from_row({"forkedFrom": written["ExpressionAttributeValues"][":ff"]})
        assert origin == ForkOrigin(PARENT, message_index=4, cut=4, parent_rewind_epoch=0)
        assert store.call_args.kwargs["session_metadata"].message_count == 4

    def test_skips_what_a_rewind_hid_and_records_the_epoch(self, env):
        # Rewound at 2, then a new turn at 4..5; fork before the new user message.
        row = {"SK": f"S#{PARENT}", "rewind": RewindState().with_cut(2, 4).to_dict()}
        stored = _conversation(3)

        result, table, copies, _, _ = _run(row, stored, f"msg-{PARENT}-4")

        assert copies[0][2] == stored[:2]
        origin = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":ff"]
        assert origin["cut"] == 2 and origin["parentRewindEpoch"] == 1

    def test_a_fork_of_a_fork_keeps_the_family_root(self, env):
        row = {"SK": f"S#{PARENT}", "rootSessionId": "the-root"}
        result, table, _, _, _ = _run(row, _conversation(2), f"msg-{PARENT}-2")

        assert result.root_session_id == "the-root"
        assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":root"] == "the-root"

    def test_carries_the_compaction_state_with_the_cut_rules_and_a_fresh_stamp(self, env):
        row = {
            "SK": f"S#{PARENT}",
            "compaction": {"checkpoint": 2, "truncationAnchor": 6.0, "summary": "s", "updatedAt": "2020-01-01T00:00:00+00:00"},
        }
        _, table, _, _, _ = _run(row, _conversation(4), f"msg-{PARENT}-4")

        compaction = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":cmp"]
        assert compaction["checkpoint"] == 2 and compaction["summary"] == "s"
        assert compaction["truncationAnchor"] == 4
        assert compaction["updatedAt"] > "2026"

    def test_moves_display_texts_to_their_new_indices(self, env):
        row = {"SK": f"S#{PARENT}", "rewind": RewindState().with_cut(2, 4).to_dict()}
        index = {"4": {"displayText": "what I typed"}, "0": {"displayText": "first"}}

        _, _, _, _, display = _run(row, _conversation(4), f"msg-{PARENT}-6", metadata_index=index)

        stored_at = sorted((c.args[2], c.args[3]) for c in display.call_args_list)
        assert stored_at == [(0, "first"), (2, "what I typed")]

    def test_forking_from_the_first_message_copies_nothing(self, env):
        result, _, copies, store, _ = _run({"SK": f"S#{PARENT}"}, _conversation(2), f"msg-{PARENT}-0")

        assert copies == []
        assert store.call_args.kwargs["session_metadata"].message_count == 0
        assert result.prefill_text == "question 0"

    @pytest.mark.parametrize(
        "row_extra, index, status",
        [
            ({"compaction": {"checkpoint": 4}}, 2, 409),
            ({"rewind": RewindState().with_cut(2, 4).to_dict()}, 2, 409),
            ({}, 1, 400),
            ({}, 99, 404),
        ],
    )
    def test_refusals(self, env, row_extra, index, status):
        with pytest.raises(RewindRefused) as refused:
            _run({"SK": f"S#{PARENT}", **row_extra}, _conversation(4), f"msg-{PARENT}-{index}")
        assert refused.value.status == status


class TestRoute:
    def _client(self, monkeypatch, enabled):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apis.app_api.sessions.routes import router
        from apis.shared.auth.dependencies import get_current_user_from_session

        if enabled:
            monkeypatch.setenv("SESSION_FORK_ENABLED", "true")
        else:
            monkeypatch.delenv("SESSION_FORK_ENABLED", raising=False)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_from_session] = lambda: MagicMock(user_id=USER)
        return TestClient(app)

    def test_404_while_the_flag_is_off(self, monkeypatch):
        response = self._client(monkeypatch, False).post(f"/sessions/{PARENT}/fork", json={"messageId": f"msg-{PARENT}-0"})

        assert response.status_code == 404

    def test_answers_the_new_session_by_alias(self, monkeypatch):
        result = svc.ForkResult(session_id="child", title="t (fork)", prefill_text="edit me", root_session_id=PARENT, copied_messages=2)
        with patch.object(svc, "fork_session", new=AsyncMock(return_value=result)):
            response = self._client(monkeypatch, True).post(f"/sessions/{PARENT}/fork", json={"messageId": f"msg-{PARENT}-2"})

        assert response.status_code == 200
        assert response.json() == {"sessionId": "child", "title": "t (fork)", "prefillText": "edit me", "rootSessionId": PARENT}
