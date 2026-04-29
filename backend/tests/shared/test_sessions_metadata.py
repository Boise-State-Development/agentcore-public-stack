"""Task 10: Sessions metadata tests (moto DynamoDB)."""

import pytest
from apis.shared.sessions.models import SessionMetadata, MessageMetadata, TokenUsage, ModelInfo


def _make_session_metadata(session_id="s1", user_id="u1", **kw):
    defaults = dict(
        sessionId=session_id, userId=user_id, title="Test Session",
        status="active", createdAt="2026-01-01T00:00:00Z",
        lastMessageAt="2026-01-01T00:00:00Z", messageCount=1,
    )
    defaults.update(kw)
    return SessionMetadata(**defaults)


def _make_message_metadata(**kw):
    defaults = dict(
        token_usage=TokenUsage(inputTokens=100, outputTokens=50, totalTokens=150),
        model_info=ModelInfo(modelId="claude-3", modelName="Claude 3"),
        cost=0.0105,
    )
    defaults.update(kw)
    return MessageMetadata(**defaults)


class TestStoreMessageMetadata:
    @pytest.mark.asyncio
    async def test_store_cost_record(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_message_metadata
        meta = _make_message_metadata()
        await store_message_metadata(session_id="s1", user_id="u1", message_id=1, message_metadata=meta)
        items = sessions_metadata_table.scan()["Items"]
        cost_items = [i for i in items if i["SK"].startswith("C#")]
        assert len(cost_items) == 1
        assert cost_items[0]["GSI_PK"] == "SESSION#s1"

    @pytest.mark.asyncio
    async def test_store_multiple_cost_records(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_message_metadata
        for i in range(3):
            await store_message_metadata(session_id="s1", user_id="u1", message_id=i, message_metadata=_make_message_metadata())
        items = sessions_metadata_table.scan()["Items"]
        cost_items = [i for i in items if i["SK"].startswith("C#")]
        assert len(cost_items) == 3


class TestStoreSessionMetadata:
    @pytest.mark.asyncio
    async def test_create_session(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, get_session_metadata
        meta = _make_session_metadata()
        await store_session_metadata(session_id="s1", user_id="u1", session_metadata=meta)
        result = await get_session_metadata("s1", "u1")
        assert result is not None
        assert result.title == "Test Session"

    @pytest.mark.asyncio
    async def test_update_session(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, get_session_metadata
        await store_session_metadata(session_id="s1", user_id="u1", session_metadata=_make_session_metadata(title="V1"))
        await store_session_metadata(session_id="s1", user_id="u1", session_metadata=_make_session_metadata(title="V2", messageCount=5))
        result = await get_session_metadata("s1", "u1")
        assert result.title == "V2"


class TestGetSessionMetadata:
    @pytest.mark.asyncio
    async def test_get_nonexistent(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import get_session_metadata
        result = await get_session_metadata("nope", "u1")
        assert result is None


class TestGetAllMessageMetadata:
    @pytest.mark.asyncio
    async def test_get_cost_records(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_message_metadata, get_all_message_metadata
        await store_message_metadata(session_id="s1", user_id="u1", message_id=1, message_metadata=_make_message_metadata())
        result = await get_all_message_metadata("s1", "u1")
        assert len(result) >= 1
        assert any(isinstance(v, dict) for v in result.values())


class TestListUserSessions:
    @pytest.mark.asyncio
    async def test_list_sessions(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, list_user_sessions
        for i in range(3):
            await store_session_metadata(
                session_id=f"s{i}", user_id="u1",
                session_metadata=_make_session_metadata(f"s{i}", lastMessageAt=f"2026-01-0{i+1}T00:00:00Z"),
            )
        sessions, token = await list_user_sessions("u1")
        assert len(sessions) == 3

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, list_user_sessions
        for i in range(5):
            await store_session_metadata(
                session_id=f"s{i}", user_id="u1",
                session_metadata=_make_session_metadata(f"s{i}", lastMessageAt=f"2026-01-0{i+1}T00:00:00Z"),
            )
        page1, token = await list_user_sessions("u1", limit=2)
        assert len(page1) == 2
        assert token is not None
        page2, _ = await list_user_sessions("u1", limit=2, next_token=token)
        assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_list_empty(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import list_user_sessions
        sessions, token = await list_user_sessions("u1")
        assert sessions == []
        assert token is None

    @pytest.mark.asyncio
    async def test_missing_env_raises(self, sessions_metadata_table, monkeypatch):
        monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
        from apis.shared.sessions.metadata import list_user_sessions
        with pytest.raises(RuntimeError):
            await list_user_sessions("u1")


class TestStoreUserDisplayText:
    """Tests for the displayText feature (D# records)."""

    @pytest.mark.asyncio
    async def test_store_and_retrieve_display_text(self, sessions_metadata_table):
        """displayText stored via D# record is merged into get_all_message_metadata."""
        from apis.shared.sessions.metadata import store_user_display_text, get_all_message_metadata

        await store_user_display_text(
            session_id="s1", user_id="u1", message_id=0, display_text="Hello world",
        )
        result = await get_all_message_metadata("s1", "u1")
        assert "0" in result
        assert result["0"]["displayText"] == "Hello world"

    @pytest.mark.asyncio
    async def test_display_text_merged_with_cost_record(self, sessions_metadata_table):
        """When both a cost record and displayText exist for the same message, they merge."""
        from apis.shared.sessions.metadata import (
            store_message_metadata, store_user_display_text, get_all_message_metadata,
        )

        await store_message_metadata(
            session_id="s1", user_id="u1", message_id=0, message_metadata=_make_message_metadata(),
        )
        await store_user_display_text(
            session_id="s1", user_id="u1", message_id=0, display_text="What is AWS?",
        )
        result = await get_all_message_metadata("s1", "u1")
        assert "0" in result
        # Should have both cost data and displayText
        assert result["0"]["displayText"] == "What is AWS?"
        assert "cost" in result["0"]

    @pytest.mark.asyncio
    async def test_display_text_without_cost_record(self, sessions_metadata_table):
        """displayText record alone creates an entry even without a matching cost record."""
        from apis.shared.sessions.metadata import store_user_display_text, get_all_message_metadata

        await store_user_display_text(
            session_id="s1", user_id="u1", message_id=2, display_text="standalone text",
        )
        result = await get_all_message_metadata("s1", "u1")
        assert "2" in result
        assert result["2"] == {"displayText": "standalone text"}

    @pytest.mark.asyncio
    async def test_display_text_sk_pattern(self, sessions_metadata_table):
        """D# records use the correct SK and GSI_SK patterns."""
        from apis.shared.sessions.metadata import store_user_display_text

        await store_user_display_text(
            session_id="s1", user_id="u1", message_id=4, display_text="test",
        )
        items = sessions_metadata_table.scan()["Items"]
        d_items = [i for i in items if i["SK"].startswith("D#")]
        assert len(d_items) == 1
        assert d_items[0]["SK"] == "D#s1#4"
        assert d_items[0]["GSI_PK"] == "SESSION#s1"
        assert d_items[0]["GSI_SK"] == "D#4"

    @pytest.mark.asyncio
    async def test_display_text_skips_preview_session(self, sessions_metadata_table):
        """Preview sessions should not persist displayText records."""
        from apis.shared.sessions.metadata import store_user_display_text

        await store_user_display_text(
            session_id="preview-abc123", user_id="u1", message_id=0, display_text="ignored",
        )
        items = sessions_metadata_table.scan()["Items"]
        d_items = [i for i in items if i["SK"].startswith("D#")]
        assert len(d_items) == 0

    @pytest.mark.asyncio
    async def test_display_text_multiple_messages(self, sessions_metadata_table):
        """Multiple displayText records in the same session are all retrievable."""
        from apis.shared.sessions.metadata import store_user_display_text, get_all_message_metadata

        await store_user_display_text(session_id="s1", user_id="u1", message_id=0, display_text="first")
        await store_user_display_text(session_id="s1", user_id="u1", message_id=2, display_text="second")
        await store_user_display_text(session_id="s1", user_id="u1", message_id=4, display_text="third")

        result = await get_all_message_metadata("s1", "u1")
        assert result["0"]["displayText"] == "first"
        assert result["2"]["displayText"] == "second"
        assert result["4"]["displayText"] == "third"

    @pytest.mark.asyncio
    async def test_display_text_user_isolation(self, sessions_metadata_table):
        """displayText from a different user should not leak into another user's query."""
        from apis.shared.sessions.metadata import store_user_display_text, get_all_message_metadata

        await store_user_display_text(session_id="s1", user_id="u1", message_id=0, display_text="user1 msg")
        await store_user_display_text(session_id="s1", user_id="u2", message_id=0, display_text="user2 msg")

        result_u1 = await get_all_message_metadata("s1", "u1")
        assert result_u1.get("0", {}).get("displayText") == "user1 msg"

    @pytest.mark.asyncio
    async def test_missing_env_raises(self, sessions_metadata_table, monkeypatch):
        """store_user_display_text raises RuntimeError when env var is missing."""
        monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
        from apis.shared.sessions.metadata import store_user_display_text
        with pytest.raises(RuntimeError):
            await store_user_display_text(
                session_id="s1", user_id="u1", message_id=0, display_text="boom",
            )


class TestUpdateSessionActivity:
    """Per-turn metadata update via targeted writes — closes the merge-write race."""

    @pytest.mark.asyncio
    async def test_increments_message_count(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            update_session_activity,
            get_session_metadata,
        )
        await ensure_session_metadata_exists("s1", "u1")
        before = await get_session_metadata("s1", "u1")
        assert before.message_count == 0

        applied = await update_session_activity(
            session_id="s1", user_id="u1", last_model="claude-3", last_temperature=0.7,
        )
        assert applied is True
        after = await get_session_metadata("s1", "u1")
        assert after.message_count == 1

        await update_session_activity(session_id="s1", user_id="u1", last_model="claude-3")
        after2 = await get_session_metadata("s1", "u1")
        assert after2.message_count == 2

    @pytest.mark.asyncio
    async def test_preserves_title_set_by_title_gen(self, sessions_metadata_table):
        """Race regression: post-stream activity update must not clobber title-gen's write."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            update_session_title,
            update_session_activity,
            get_session_metadata,
        )
        await ensure_session_metadata_exists("s1", "u1")
        await update_session_title("s1", "u1", "My Generated Title")
        await update_session_activity(
            session_id="s1", user_id="u1", last_model="claude-3", last_temperature=0.5,
        )
        result = await get_session_metadata("s1", "u1")
        assert result.title == "My Generated Title"

    @pytest.mark.asyncio
    async def test_preserves_pending_interrupts(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            add_pending_interrupt,
            update_session_activity,
            get_pending_interrupts,
        )
        from apis.shared.sessions.models import PendingInterrupt

        await ensure_session_metadata_exists("s1", "u1")
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack", createdAt="2026-04-25T00:00:00Z",
            ),
        )
        await update_session_activity(session_id="s1", user_id="u1", last_model="claude-3")
        interrupts = await get_pending_interrupts("s1", "u1")
        assert len(interrupts) == 1
        assert interrupts[0].interrupt_id == "i1"

    @pytest.mark.asyncio
    async def test_preserves_assistant_id_in_preferences(self, sessions_metadata_table):
        """assistant_id set by the assistant-attach flow must survive per-turn updates."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            store_session_metadata,
            update_session_activity,
            get_session_metadata,
        )
        from apis.shared.sessions.models import SessionMetadata, SessionPreferences

        await ensure_session_metadata_exists("s1", "u1")
        existing = await get_session_metadata("s1", "u1")
        seeded = SessionMetadata(
            sessionId="s1", userId="u1",
            title=existing.title, status="active",
            createdAt=existing.created_at,
            lastMessageAt=existing.last_message_at,
            messageCount=existing.message_count,
            preferences=SessionPreferences(assistantId="asst-abc"),
        )
        await store_session_metadata("s1", "u1", seeded)

        await update_session_activity(
            session_id="s1", user_id="u1", last_model="claude-3", last_temperature=0.5,
        )
        result = await get_session_metadata("s1", "u1")
        assert result.preferences.assistant_id == "asst-abc"
        assert result.preferences.last_model == "claude-3"
        assert result.preferences.last_temperature == 0.5

    @pytest.mark.asyncio
    async def test_rotates_sk_to_new_timestamp(self, sessions_metadata_table):
        """SK rotation keeps recency listing correct — only one row remains."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            update_session_activity,
        )
        await ensure_session_metadata_exists("s1", "u1")
        items = sessions_metadata_table.scan()["Items"]
        s_items = [i for i in items if i["SK"].startswith("S#ACTIVE#")]
        assert len(s_items) == 1
        old_sk = s_items[0]["SK"]

        await update_session_activity(session_id="s1", user_id="u1", last_model="claude-3")

        items = sessions_metadata_table.scan()["Items"]
        s_items_after = [i for i in items if i["SK"].startswith("S#ACTIVE#")]
        assert len(s_items_after) == 1
        assert s_items_after[0]["SK"] != old_sk

    @pytest.mark.asyncio
    async def test_self_heals_when_row_missing(self, sessions_metadata_table):
        """If pre-create failed (or row was deleted), update self-heals via ensure_session_metadata_exists."""
        from apis.shared.sessions.metadata import update_session_activity, get_session_metadata
        applied = await update_session_activity(
            session_id="never-pre-created", user_id="u1", last_model="claude-3",
        )
        assert applied is True
        result = await get_session_metadata("never-pre-created", "u1")
        assert result is not None
        assert result.message_count == 1

    @pytest.mark.asyncio
    async def test_noop_for_preview_session(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import update_session_activity
        applied = await update_session_activity(
            session_id="preview-abc", user_id="u1", last_model="claude-3",
        )
        assert applied is False
        items = sessions_metadata_table.scan()["Items"]
        assert items == []


class TestEnsureSessionMetadataExists:
    @pytest.mark.asyncio
    async def test_repeated_calls_do_not_create_duplicates(self, sessions_metadata_table):
        """Regression: each turn calls ensure_session_metadata_exists; the SK
        encodes a timestamp, so a put-with-conditional cannot gate creation
        and would produce one duplicate row per turn (sidebar duplication bug).
        """
        from apis.shared.sessions.metadata import ensure_session_metadata_exists

        first = await ensure_session_metadata_exists("s1", "u1")
        second = await ensure_session_metadata_exists("s1", "u1")
        third = await ensure_session_metadata_exists("s1", "u1")

        assert first is True
        assert second is False
        assert third is False

        items = sessions_metadata_table.scan()["Items"]
        s_items = [i for i in items if i["SK"].startswith("S#ACTIVE#") and i.get("sessionId") == "s1"]
        assert len(s_items) == 1

    @pytest.mark.asyncio
    async def test_survives_sk_rotation(self, sessions_metadata_table):
        """After update_session_activity rotates the SK, a subsequent ensure
        call must still recognize the session via the GSI and skip the put.
        """
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            update_session_activity,
        )

        await ensure_session_metadata_exists("s1", "u1")
        await update_session_activity(session_id="s1", user_id="u1", last_model="claude-3")

        again = await ensure_session_metadata_exists("s1", "u1")
        assert again is False

        items = sessions_metadata_table.scan()["Items"]
        s_items = [i for i in items if i["SK"].startswith("S#ACTIVE#") and i.get("sessionId") == "s1"]
        assert len(s_items) == 1


class TestAddPendingInterruptListAppend:
    """list_append-based persistence — race-free with no read-modify-write."""

    @pytest.mark.asyncio
    async def test_first_interrupt(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, add_pending_interrupt, get_pending_interrupts,
        )
        from apis.shared.sessions.models import PendingInterrupt

        await ensure_session_metadata_exists("s1", "u1")
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack", createdAt="2026-04-25T00:00:00Z",
            ),
        )
        interrupts = await get_pending_interrupts("s1", "u1")
        assert len(interrupts) == 1
        assert interrupts[0].interrupt_id == "i1"
        assert interrupts[0].provider_id == "slack"

    @pytest.mark.asyncio
    async def test_two_distinct_interrupts_accumulate(self, sessions_metadata_table):
        """Two adds for different ids accumulate — list_append is atomic in DynamoDB."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, add_pending_interrupt, get_pending_interrupts,
        )
        from apis.shared.sessions.models import PendingInterrupt

        await ensure_session_metadata_exists("s1", "u1")
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack", createdAt="2026-04-25T00:00:00Z",
            ),
        )
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i2", providerId="gmail", createdAt="2026-04-25T00:00:01Z",
            ),
        )
        interrupts = await get_pending_interrupts("s1", "u1")
        ids = {p.interrupt_id for p in interrupts}
        assert ids == {"i1", "i2"}

    @pytest.mark.asyncio
    async def test_reemit_dedupes_on_read_last_write_wins(self, sessions_metadata_table):
        """Same id added twice → one entry on read, last write's payload survives."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, add_pending_interrupt, get_pending_interrupts,
        )
        from apis.shared.sessions.models import PendingInterrupt

        await ensure_session_metadata_exists("s1", "u1")
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack", createdAt="2026-04-25T00:00:00Z",
            ),
        )
        await add_pending_interrupt(
            session_id="s1", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack",
                triggeringMessageId="msg-7",
                createdAt="2026-04-25T00:00:05Z",
            ),
        )
        interrupts = await get_pending_interrupts("s1", "u1")
        assert len(interrupts) == 1
        assert interrupts[0].interrupt_id == "i1"
        assert interrupts[0].triggering_message_id == "msg-7"
        assert interrupts[0].created_at == "2026-04-25T00:00:05Z"

    @pytest.mark.asyncio
    async def test_noop_when_session_missing(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import add_pending_interrupt, get_pending_interrupts
        from apis.shared.sessions.models import PendingInterrupt

        await add_pending_interrupt(
            session_id="never-created", user_id="u1",
            interrupt=PendingInterrupt(
                interruptId="i1", providerId="slack", createdAt="2026-04-25T00:00:00Z",
            ),
        )
        interrupts = await get_pending_interrupts("never-created", "u1")
        assert interrupts == []


class TestInterruptsFromDynamoDedupe:
    """Read-side dedupe collapses duplicates produced by list_append re-emits."""

    def test_dedupe_by_id_last_write_wins(self):
        from apis.shared.sessions.metadata import _interrupts_from_dynamo
        raw = [
            {"interruptId": "i1", "providerId": "slack", "createdAt": "2026-04-25T00:00:00Z"},
            {"interruptId": "i2", "providerId": "gmail", "createdAt": "2026-04-25T00:00:01Z"},
            {"interruptId": "i1", "providerId": "slack",
             "triggeringMessageId": "msg-7", "createdAt": "2026-04-25T00:00:05Z"},
        ]
        result = _interrupts_from_dynamo(raw)
        assert [p.interrupt_id for p in result] == ["i1", "i2"]
        i1 = next(p for p in result if p.interrupt_id == "i1")
        assert i1.triggering_message_id == "msg-7"
        assert i1.created_at == "2026-04-25T00:00:05Z"

    def test_skips_unparseable_entries(self):
        from apis.shared.sessions.metadata import _interrupts_from_dynamo
        raw = [
            {"interruptId": "i1", "providerId": "slack", "createdAt": "2026-04-25T00:00:00Z"},
            {"missing": "required-fields"},
            "not a dict",
        ]
        result = _interrupts_from_dynamo(raw)
        assert len(result) == 1
        assert result[0].interrupt_id == "i1"

    def test_empty_input(self):
        from apis.shared.sessions.metadata import _interrupts_from_dynamo
        assert _interrupts_from_dynamo(None) == []
        assert _interrupts_from_dynamo([]) == []
        assert _interrupts_from_dynamo("not a list") == []


class TestPausedTurnSnapshot:
    """PausedTurnSnapshot persistence — singleton, idempotent, round-trippable.

    The snapshot is the durable contract that lets a refresh / cache eviction
    resume a paused agent turn — without it, the resume rebuilds an agent
    with an empty tool registry and the paused tool call has nothing to
    resume against.
    """

    @pytest.mark.asyncio
    async def test_set_get_round_trip(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, set_paused_turn, get_paused_turn,
        )
        from apis.shared.sessions.models import PausedTurnSnapshot

        await ensure_session_metadata_exists("s1", "u1")
        snap = PausedTurnSnapshot(
            enabledTools=["calendar", "gmail"], modelId="claude-sonnet-4-6",
            provider="bedrock", temperature=0.2, systemPrompt="prompt-text",
            cachingEnabled=True, maxTokens=4096,
            capturedAt="2026-04-25T00:00:00Z", expiresAt="2026-04-25T01:00:00Z",
        )
        await set_paused_turn("s1", "u1", snap)
        got = await get_paused_turn("s1", "u1")
        assert got is not None
        assert got.enabled_tools == ["calendar", "gmail"]
        assert got.model_id == "claude-sonnet-4-6"
        assert got.system_prompt == "prompt-text"
        assert got.temperature == 0.2
        assert got.caching_enabled is True

    @pytest.mark.asyncio
    async def test_idempotent_overwrite(self, sessions_metadata_table):
        """Multiple OAuth interrupts in one turn share a single snapshot —
        re-writing replaces in place rather than accumulating."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, set_paused_turn, get_paused_turn,
        )
        from apis.shared.sessions.models import PausedTurnSnapshot

        await ensure_session_metadata_exists("s1", "u1")
        first = PausedTurnSnapshot(
            enabledTools=["calendar"], capturedAt="2026-04-25T00:00:00Z",
            expiresAt="2026-04-25T01:00:00Z",
        )
        second = PausedTurnSnapshot(
            enabledTools=["calendar", "gmail"], capturedAt="2026-04-25T00:00:01Z",
            expiresAt="2026-04-25T01:00:01Z",
        )
        await set_paused_turn("s1", "u1", first)
        await set_paused_turn("s1", "u1", second)
        got = await get_paused_turn("s1", "u1")
        assert got is not None
        assert got.enabled_tools == ["calendar", "gmail"]
        assert got.captured_at == "2026-04-25T00:00:01Z"

    @pytest.mark.asyncio
    async def test_clear_removes_snapshot(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, set_paused_turn,
            get_paused_turn, clear_paused_turn,
        )
        from apis.shared.sessions.models import PausedTurnSnapshot

        await ensure_session_metadata_exists("s1", "u1")
        await set_paused_turn(
            "s1", "u1",
            PausedTurnSnapshot(
                enabledTools=["calendar"], capturedAt="2026-04-25T00:00:00Z",
                expiresAt="2026-04-25T01:00:00Z",
            ),
        )
        assert await get_paused_turn("s1", "u1") is not None
        await clear_paused_turn("s1", "u1")
        assert await get_paused_turn("s1", "u1") is None

    @pytest.mark.asyncio
    async def test_clear_is_noop_when_already_clear(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, clear_paused_turn, get_paused_turn,
        )

        await ensure_session_metadata_exists("s1", "u1")
        await clear_paused_turn("s1", "u1")
        assert await get_paused_turn("s1", "u1") is None

    @pytest.mark.asyncio
    async def test_set_noop_when_session_missing(self, sessions_metadata_table):
        """Preview/anonymous sessions don't have a metadata row — write must
        not crash and a subsequent get returns None."""
        from apis.shared.sessions.metadata import set_paused_turn, get_paused_turn
        from apis.shared.sessions.models import PausedTurnSnapshot

        await set_paused_turn(
            "never-created", "u1",
            PausedTurnSnapshot(
                enabledTools=["calendar"], capturedAt="2026-04-25T00:00:00Z",
                expiresAt="2026-04-25T01:00:00Z",
            ),
        )
        assert await get_paused_turn("never-created", "u1") is None

    @pytest.mark.asyncio
    async def test_paused_turn_independent_of_pending_interrupts(self, sessions_metadata_table):
        """``paused_turn`` and ``pending_interrupts`` live on the same row
        but their lifecycles don't intrude on each other — clearing one
        leaves the other intact."""
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists, set_paused_turn, clear_paused_turn,
            add_pending_interrupt, get_pending_interrupts, get_paused_turn,
        )
        from apis.shared.sessions.models import PausedTurnSnapshot, PendingInterrupt

        await ensure_session_metadata_exists("s1", "u1")
        await set_paused_turn(
            "s1", "u1",
            PausedTurnSnapshot(
                enabledTools=["calendar"], capturedAt="2026-04-25T00:00:00Z",
                expiresAt="2026-04-25T01:00:00Z",
            ),
        )
        await add_pending_interrupt(
            "s1", "u1",
            PendingInterrupt(
                interruptId="i1", providerId="calendar", createdAt="2026-04-25T00:00:00Z",
            ),
        )

        await clear_paused_turn("s1", "u1")
        assert await get_paused_turn("s1", "u1") is None
        interrupts = await get_pending_interrupts("s1", "u1")
        assert len(interrupts) == 1
        assert interrupts[0].interrupt_id == "i1"
