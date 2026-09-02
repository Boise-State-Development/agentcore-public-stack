"""A session id must never be forked across two users.

Prod, 2026-08-31: someone opened the CIO's `/s/{sessionId}` link. The metadata
read 404'd, the SPA treated the session as new, and the turn that followed
created a SECOND metadata row on the same session id under the second user —
`ensure_session_metadata_exists`'s `attribute_not_exists(PK)` guard cannot see
it, because the new row has a different PK.

Not a confidentiality bug: conversation content is keyed by actor id in
AgentCore Memory, so the second user only ever saw an empty thread. The damage
was the duplicate row, the billing attached to it, and the original owner's
session resolving non-deterministically between the two rows afterwards.
"""

import pytest

from apis.shared.sessions.models import SessionMetadata


def _meta(session_id="s1", user_id="owner", **kw):
    defaults = dict(
        sessionId=session_id, userId=user_id, title="Test Session",
        status="active", createdAt="2026-01-01T00:00:00Z",
        lastMessageAt="2026-01-01T00:00:00Z", messageCount=1,
    )
    defaults.update(kw)
    return SessionMetadata(**defaults)


def _put_forked_row(table, session_id: str, user_id: str, title: str) -> None:
    """Write a second META row for a session id, bypassing the write path.

    Reproduces the rows the platform created before `session_owned_by_other_user`
    existed. Both rows carry identical GSI_PK/GSI_SK, so DynamoDB returns them
    in an unspecified order — which is exactly the condition the item-scan in
    `_get_session_by_gsi` has to survive.
    """
    table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"S#{session_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": "META",
            "sessionId": session_id,
            "userId": user_id,
            "title": title,
            "status": "active",
            "createdAt": "2026-01-01T00:00:00Z",
            "lastMessageAt": "2026-01-01T00:00:00Z",
            "messageCount": 0,
            "starred": False,
            "tags": [],
        }
    )


class TestSessionOwnedByOtherUser:
    @pytest.mark.asyncio
    async def test_false_when_no_session_exists(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import session_owned_by_other_user
        assert await session_owned_by_other_user("nope", "someone") is False

    @pytest.mark.asyncio
    async def test_false_for_the_owner(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            store_session_metadata,
            session_owned_by_other_user,
        )
        await store_session_metadata(session_id="s1", user_id="owner", session_metadata=_meta())
        assert await session_owned_by_other_user("s1", "owner") is False

    @pytest.mark.asyncio
    async def test_true_for_a_stranger(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            store_session_metadata,
            session_owned_by_other_user,
        )
        await store_session_metadata(session_id="s1", user_id="owner", session_metadata=_meta())
        assert await session_owned_by_other_user("s1", "stranger") is True


class TestEnsureRefusesToFork:
    @pytest.mark.asyncio
    async def test_stranger_does_not_get_a_second_metadata_row(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import (
            store_session_metadata,
            ensure_session_metadata_exists,
        )
        await store_session_metadata(session_id="s1", user_id="owner", session_metadata=_meta())

        created = await ensure_session_metadata_exists("s1", "stranger")

        assert created is False
        metas = [
            i for i in sessions_metadata_table.scan()["Items"]
            if i.get("GSI_SK") == "META" and i.get("sessionId") == "s1"
        ]
        assert len(metas) == 1
        assert metas[0]["userId"] == "owner"

    @pytest.mark.asyncio
    async def test_owner_is_unaffected(self, sessions_metadata_table):
        """The guard must not block the legitimate first-turn create."""
        from apis.shared.sessions.metadata import ensure_session_metadata_exists
        assert await ensure_session_metadata_exists("fresh", "owner") is True


class TestLookupIsDeterministicAcrossAnExistingFork:
    """Forked rows already exist in prod, so the read path has to cope."""

    @pytest.mark.asyncio
    async def test_each_user_resolves_to_their_own_row(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, get_session_metadata

        await store_session_metadata(
            session_id="s1", user_id="owner",
            session_metadata=_meta(title="Owner's conversation"),
        )
        # The stranger's row is written RAW. The write path now refuses to
        # create it, so this is the only way to reproduce what is already
        # sitting in prod from before the guard existed.
        _put_forked_row(sessions_metadata_table, "s1", "stranger", "New Conversation")

        owner_view = await get_session_metadata("s1", "owner")
        stranger_view = await get_session_metadata("s1", "stranger")

        # Before the item-scan fix this depended on which row items[0] returned,
        # and the owner could lose their own session.
        assert owner_view is not None
        assert owner_view.title == "Owner's conversation"
        assert stranger_view is not None
        assert stranger_view.title == "New Conversation"

    @pytest.mark.asyncio
    async def test_third_party_still_sees_nothing(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import store_session_metadata, get_session_metadata
        await store_session_metadata(session_id="s1", user_id="owner", session_metadata=_meta())
        _put_forked_row(sessions_metadata_table, "s1", "stranger", "New Conversation")
        assert await get_session_metadata("s1", "outsider") is None
