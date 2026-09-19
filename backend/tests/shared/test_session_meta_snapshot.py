"""One read of the session META row, shared across the preamble (PR-2).

Measured on dev 2026-09-19 (docs/specs/turn-latency-preamble.md): a warm turn
read this one item EIGHT times before the first model call, and a GSI query
from an AgentCore Runtime container costs ~53ms — so those reads were ~445ms
of a ~455ms stage.

What these tests pin is not the speed (a moto table is not us-west-2) but the
two things that make the speed safe to take:

1. the snapshot answers exactly what the separate reads answered, including
   the cross-user fork case the ownership probe exists for, and
2. `snapshot=None` leaves every other caller on its original per-call read,
   because an implicit cache of session state is a bug this repo has shipped
   twice (#741, #751).
"""

import pytest

SESSION = "sess-1"
OWNER = "owner-1"
OTHER = "other-1"


def _put_meta(table, session_id=SESSION, user_id=OWNER, **extra):
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "sessionId": session_id,
        "userId": user_id,
        "title": "Test Session",
        "status": "active",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastMessageAt": "2026-01-01T00:00:00Z",
        "messageCount": 0,
        "starred": False,
        "tags": [],
    }
    item.update(extra)
    table.put_item(Item=item)
    return item


class TestLoadSessionMeta:
    @pytest.mark.asyncio
    async def test_returns_the_row_for_its_owner(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import load_session_meta

        _put_meta(sessions_metadata_table, title="Mine")

        snap = await load_session_meta(SESSION, OWNER)

        assert snap.owned_by_other is False
        assert snap.row is not None
        assert snap.row["title"] == "Mine"

    @pytest.mark.asyncio
    async def test_absent_session_is_row_none_and_not_owned_by_other(
        self, sessions_metadata_table
    ):
        """`row is None` must mean "no row", distinctly from "someone else's" —
        collapsing the two is the ambiguity `session_owned_by_other_user` was
        written to resolve."""
        from apis.shared.sessions.metadata import load_session_meta

        snap = await load_session_meta("nope", OWNER)

        assert snap.row is None
        assert snap.owned_by_other is False

    @pytest.mark.asyncio
    async def test_someone_elses_session_is_owned_by_other(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import load_session_meta

        _put_meta(sessions_metadata_table, user_id=OTHER)

        snap = await load_session_meta(SESSION, OWNER)

        assert snap.owned_by_other is True
        assert snap.row is None

    @pytest.mark.asyncio
    async def test_a_forked_session_still_resolves_to_the_caller_s_own_row(
        self, sessions_metadata_table
    ):
        """Prod 2026-08-31 left two META rows sharing GSI_PK/GSI_SK, returned in
        an unspecified order. The owner must still get THEIR row, not a coin
        flip — the same item-scan `_get_session_by_gsi` does."""
        from apis.shared.sessions.metadata import load_session_meta

        _put_meta(sessions_metadata_table, user_id=OTHER, title="Fork")
        _put_meta(sessions_metadata_table, user_id=OWNER, title="Mine")

        snap = await load_session_meta(SESSION, OWNER)

        assert snap.owned_by_other is False
        assert snap.row["title"] == "Mine"

    @pytest.mark.asyncio
    async def test_agrees_with_the_two_reads_it_replaces(self, sessions_metadata_table):
        """The consolidation must be behaviour-preserving, so assert it against
        the originals rather than against a remembered description of them."""
        from apis.shared.sessions.metadata import (
            _get_session_by_gsi,
            load_session_meta,
            session_owned_by_other_user,
        )
        import boto3

        _put_meta(sessions_metadata_table, user_id=OTHER)
        _put_meta(sessions_metadata_table, user_id=OWNER)
        table = boto3.resource("dynamodb").Table("test-sessions-metadata")

        snap = await load_session_meta(SESSION, OWNER)

        assert snap.owned_by_other == await session_owned_by_other_user(SESSION, OWNER)
        assert snap.row == await _get_session_by_gsi(SESSION, OWNER, table)

    @pytest.mark.asyncio
    async def test_a_read_failure_degrades_to_nothing_known_nothing_blocked(
        self, monkeypatch, sessions_metadata_table
    ):
        """Fail-open in both fields, matching what the two reads already did:
        the ownership probe returned False and the row read returned None."""
        import boto3

        from apis.shared.sessions import metadata

        def _boom(*a, **kw):
            raise RuntimeError("dynamodb is down")

        # `boto3` is imported inside the function, so the module attribute is
        # what the call resolves against.
        monkeypatch.setattr(boto3, "resource", _boom)

        snap = await metadata.load_session_meta(SESSION, OWNER)

        assert snap.row is None
        assert snap.owned_by_other is False


class TestSnapshotThreading:
    """`snapshot=` must be the ONLY thing that skips the read."""

    @pytest.mark.asyncio
    async def test_helpers_use_the_snapshot_instead_of_reading(
        self, monkeypatch, sessions_metadata_table
    ):
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table, pausedTurn={"foo": "bar"})
        snap = await metadata.load_session_meta(SESSION, OWNER)

        reads = []

        async def _counted(session_id, user_id, table):
            reads.append(session_id)
            return None

        monkeypatch.setattr(metadata, "_get_session_by_gsi", _counted)

        await metadata.clear_paused_turn(SESSION, OWNER, snapshot=snap)

        assert reads == [], "a supplied snapshot must not be re-read"

    @pytest.mark.asyncio
    async def test_omitting_the_snapshot_still_reads(
        self, monkeypatch, sessions_metadata_table
    ):
        """The default path is what every non-preamble caller gets, and it must
        keep reading — a snapshot that leaked into those callers would be the
        stale-session-state bug CLAUDE.md names."""
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table)
        reads = []
        original = metadata._get_session_by_gsi

        async def _counted(session_id, user_id, table):
            reads.append(session_id)
            return await original(session_id, user_id, table)

        monkeypatch.setattr(metadata, "_get_session_by_gsi", _counted)

        await metadata.clear_paused_turn(SESSION, OWNER)

        assert reads == [SESSION]

    @pytest.mark.asyncio
    async def test_a_snapshot_with_no_row_is_an_answer_not_a_cache_miss(
        self, monkeypatch, sessions_metadata_table
    ):
        """A brand-new session has `row=None`. If that were treated as "nothing
        prefetched" the helper would fall back to a read, and the first turn of
        every conversation would silently keep the old cost."""
        from apis.shared.sessions import metadata

        snap = await metadata.load_session_meta("brand-new", OWNER)
        assert snap.row is None

        reads = []

        async def _counted(session_id, user_id, table):
            reads.append(session_id)
            return None

        monkeypatch.setattr(metadata, "_get_session_by_gsi", _counted)

        await metadata.clear_paused_turn("brand-new", OWNER, snapshot=snap)

        assert reads == []


class TestBehaviourPreserved:
    """The clears must still clear, and still no-op when there is nothing to do."""

    @pytest.mark.asyncio
    async def test_clear_paused_turn_removes_the_snapshot_attribute(
        self, sessions_metadata_table
    ):
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table, pausedTurn={"interruptId": "i-1"})
        snap = await metadata.load_session_meta(SESSION, OWNER)

        await metadata.clear_paused_turn(SESSION, OWNER, snapshot=snap)

        row = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )["Item"]
        assert "pausedTurn" not in row

    @pytest.mark.asyncio
    async def test_clear_interrupted_turn_still_pops_the_reason(
        self, sessions_metadata_table
    ):
        """The return value drives the one-turn interruption note in the prompt,
        so a snapshot that broke it would silently change what the model sees."""
        from apis.shared.sessions import metadata

        _put_meta(
            sessions_metadata_table,
            lastTurnInterrupted=True,
            lastTurnInterruptReason="user_stopped",
        )
        snap = await metadata.load_session_meta(SESSION, OWNER)

        reason = await metadata.clear_interrupted_turn(SESSION, OWNER, snapshot=snap)

        assert reason == "user_stopped"
        row = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )["Item"]
        assert "lastTurnInterrupted" not in row

    @pytest.mark.asyncio
    async def test_pop_pending_attachments_still_returns_and_clears(
        self, sessions_metadata_table
    ):
        from datetime import datetime, timezone

        from apis.shared.sessions import metadata

        _put_meta(
            sessions_metadata_table,
            pendingAttachmentUploadIds=["u-1", "u-2"],
            pendingAttachmentsAt=datetime.now(timezone.utc).isoformat(),
        )
        snap = await metadata.load_session_meta(SESSION, OWNER)

        got = await metadata.pop_pending_attachments(SESSION, OWNER, snapshot=snap)

        assert got == ["u-1", "u-2"]
        row = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )["Item"]
        assert "pendingAttachmentUploadIds" not in row

    @pytest.mark.asyncio
    async def test_a_clean_row_writes_nothing(self, sessions_metadata_table):
        """The short-circuit is what makes the warm path cheap: four of the five
        clears find nothing and must not spend a write proving it."""
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table)
        snap = await metadata.load_session_meta(SESSION, OWNER)
        before = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )["Item"]

        await metadata.clear_paused_turn(SESSION, OWNER, snapshot=snap)
        await metadata.clear_pending_interrupts(SESSION, OWNER, snapshot=snap)
        await metadata.clear_truncated_turn(SESSION, OWNER, snapshot=snap)
        await metadata.clear_interrupted_turn(SESSION, OWNER, snapshot=snap)

        after = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )["Item"]
        assert before == after

    @pytest.mark.asyncio
    async def test_ensure_metadata_skips_creation_when_the_snapshot_has_a_row(
        self, sessions_metadata_table
    ):
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table)
        snap = await metadata.load_session_meta(SESSION, OWNER)

        assert await metadata.ensure_session_metadata_exists(
            SESSION, OWNER, snapshot=snap
        ) is False

    @pytest.mark.asyncio
    async def test_ensure_metadata_creates_for_a_genuinely_new_session(
        self, sessions_metadata_table
    ):
        """`True` is the first-turn signal that fires title generation, so the
        snapshot path has to keep returning it."""
        from apis.shared.sessions import metadata

        snap = await metadata.load_session_meta("fresh", OWNER)

        assert await metadata.ensure_session_metadata_exists(
            "fresh", OWNER, snapshot=snap
        ) is True

    @pytest.mark.asyncio
    async def test_ensure_metadata_refuses_to_fork_a_session_it_does_not_own(
        self, sessions_metadata_table
    ):
        """The backstop against the prod 2026-08-31 fork. It previously cost a
        SECOND query; the snapshot already carries the answer, and losing the
        guard while making it faster would be the worst possible trade."""
        from apis.shared.sessions import metadata

        _put_meta(sessions_metadata_table, user_id=OTHER)
        snap = await metadata.load_session_meta(SESSION, OWNER)

        created = await metadata.ensure_session_metadata_exists(
            SESSION, OWNER, snapshot=snap
        )

        assert created is False
        resp = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{OWNER}", "SK": f"S#{SESSION}"}
        )
        assert "Item" not in resp, "a second row would fork the session id"
