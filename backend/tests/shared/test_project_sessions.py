"""ProjectSessionIndex (GSI5) writes and the per-member task list (shared-projects PR-1.6).

GSI5 is GSI4 scoped to one project: the same recency sort key, present exactly
when GSI4 is, but only on a session bound to a project (``preferences.projectId``).
"""

import pytest
from botocore.exceptions import ClientError

from apis.shared.sessions.models import SessionMetadata, SessionPreferences

PROJECT = "prj_a"
OTHER_PROJECT = "prj_b"


def _meta(session_id="s1", user_id="u1", project_id=PROJECT, **kw) -> SessionMetadata:
    fields = dict(
        sessionId=session_id, userId=user_id, title="Task", status="active",
        createdAt="2026-09-01T00:00:00Z", lastMessageAt="2026-09-01T00:00:00Z", messageCount=1,
    )
    if project_id:
        fields["preferences"] = SessionPreferences(assistantId="ast-h", projectId=project_id)
    fields.update(kw)
    return SessionMetadata(**fields)


def _row(table, session_id="s1"):
    rows = [i for i in table.scan()["Items"] if i.get("GSI_PK") == f"SESSION#{session_id}" and i.get("GSI_SK") == "META"]
    assert len(rows) == 1
    return rows[0]


async def _store(meta: SessionMetadata):
    from apis.shared.sessions.metadata import store_session_metadata
    await store_session_metadata(session_id=meta.session_id, user_id=meta.user_id, session_metadata=meta)


class TestGsi5Writes:
    @pytest.mark.asyncio
    async def test_project_session_gets_gsi5_beside_gsi4(self, sessions_metadata_table):
        await _store(_meta())
        row = _row(sessions_metadata_table)
        assert row["GSI5_PK"] == f"PROJECT#{PROJECT}#USER#u1"
        assert row["GSI5_SK"] == row["GSI4_SK"] == "2026-09-01T00:00:00Z#s1"

    @pytest.mark.asyncio
    async def test_plain_session_has_no_gsi5(self, sessions_metadata_table):
        await _store(_meta(project_id=None))
        row = _row(sessions_metadata_table)
        assert "GSI4_PK" in row and "GSI5_PK" not in row

    @pytest.mark.asyncio
    async def test_binding_an_existing_session_adds_gsi5(self, sessions_metadata_table):
        """The chat route binds a harness to an existing row (in-place update path)."""
        await _store(_meta(project_id=None))
        await _store(_meta(lastMessageAt="2026-09-02T00:00:00Z"))
        row = _row(sessions_metadata_table)
        assert row["GSI5_SK"] == "2026-09-02T00:00:00Z#s1"

    @pytest.mark.asyncio
    async def test_write_without_preferences_keeps_the_rows_project(self, sessions_metadata_table):
        """A write that leaves ``preferences`` untouched must not drop the session from its project."""
        await _store(_meta())
        await _store(_meta(project_id=None, title="Renamed", lastMessageAt="2026-09-03T00:00:00Z"))
        row = _row(sessions_metadata_table)
        assert row["preferences"]["projectId"] == PROJECT
        assert row["GSI5_SK"] == "2026-09-03T00:00:00Z#s1"

    @pytest.mark.asyncio
    async def test_read_modify_write_does_not_carry_stale_index_keys(self, sessions_metadata_table):
        """get_session_metadata must not surface index keys as extras that a later write replays."""
        from apis.shared.sessions.metadata import get_session_metadata

        await _store(_meta())
        read = await get_session_metadata("s1", "u1")
        assert not {"GSI4_PK", "GSI4_SK", "GSI5_PK", "GSI5_SK"} & set(read.model_extra or {})

        await _store(read.model_copy(update={"last_message_at": "2026-09-04T00:00:00Z"}))
        assert _row(sessions_metadata_table)["GSI5_SK"] == "2026-09-04T00:00:00Z#s1"

    @pytest.mark.asyncio
    async def test_store_as_deleted_removes_both_index_keys(self, sessions_metadata_table):
        await _store(_meta())
        await _store(_meta(status="deleted", deleted=True, deletedAt="2026-09-05T00:00:00Z"))
        row = _row(sessions_metadata_table)
        assert not {"GSI4_PK", "GSI4_SK", "GSI5_PK", "GSI5_SK"} & set(row)

    @pytest.mark.asyncio
    async def test_activity_advances_gsi5(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import update_session_activity

        await _store(_meta())
        assert await update_session_activity(session_id="s1", user_id="u1", last_model="m") is True
        row = _row(sessions_metadata_table)
        assert row["GSI5_SK"] == row["GSI4_SK"] != "2026-09-01T00:00:00Z#s1"
        assert row["GSI5_PK"] == f"PROJECT#{PROJECT}#USER#u1"

    @pytest.mark.asyncio
    async def test_activity_on_a_plain_session_writes_no_gsi5(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import update_session_activity

        await _store(_meta(project_id=None))
        await update_session_activity(session_id="s1", user_id="u1", last_model="m")
        assert "GSI5_PK" not in _row(sessions_metadata_table)

    @pytest.mark.asyncio
    async def test_activity_migrating_a_legacy_project_row_writes_gsi5(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import update_session_activity

        sessions_metadata_table.put_item(Item={
            "PK": "USER#u1", "SK": "S#ACTIVE#2026-09-01T00:00:00Z#s1",
            "GSI_PK": "SESSION#s1", "GSI_SK": "META",
            "sessionId": "s1", "userId": "u1", "title": "T", "status": "active",
            "createdAt": "2026-09-01T00:00:00Z", "lastMessageAt": "2026-09-01T00:00:00Z", "messageCount": 1,
            "preferences": {"projectId": PROJECT},
        })
        await update_session_activity(session_id="s1", user_id="u1", last_model="m")
        row = _row(sessions_metadata_table)
        assert row["SK"] == "S#s1"
        assert row["GSI5_SK"] == row["GSI4_SK"]

    @pytest.mark.asyncio
    async def test_soft_delete_drops_gsi5(self, sessions_metadata_table):
        from apis.app_api.sessions.services.session_service import SessionService

        await _store(_meta())
        assert await SessionService().delete_session("u1", "s1") is True
        row = _row(sessions_metadata_table)
        assert row["status"] == "deleted"
        assert not {"GSI4_PK", "GSI5_PK", "GSI5_SK"} & set(row)


class TestListProjectSessions:
    @pytest.mark.asyncio
    async def test_only_the_callers_sessions_in_that_project_newest_first(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import list_project_sessions

        await _store(_meta("old", lastMessageAt="2026-09-01T00:00:00Z"))
        await _store(_meta("new", lastMessageAt="2026-09-02T00:00:00Z"))
        await _store(_meta("elsewhere", project_id=OTHER_PROJECT))
        await _store(_meta("plain", project_id=None))
        await _store(_meta("theirs", user_id="u2"))

        sessions, token = await list_project_sessions("u1", PROJECT)
        assert [s.session_id for s in sessions] == ["new", "old"]
        assert token is None

    @pytest.mark.asyncio
    async def test_paginates_with_a_value_cursor(self, sessions_metadata_table):
        from apis.shared.sessions.metadata import list_project_sessions

        for day in range(1, 6):
            await _store(_meta(f"s{day}", lastMessageAt=f"2026-09-0{day}T00:00:00Z"))

        seen, token = [], None
        while True:
            page, token = await list_project_sessions("u1", PROJECT, limit=2, next_token=token)
            seen += [s.session_id for s in page]
            if not token:
                break
        assert seen == ["s5", "s4", "s3", "s2", "s1"]

    @pytest.mark.asyncio
    async def test_deleted_sessions_leave_the_list(self, sessions_metadata_table):
        from apis.app_api.sessions.services.session_service import SessionService
        from apis.shared.sessions.metadata import list_project_sessions

        await _store(_meta("s1"))
        await SessionService().delete_session("u1", "s1")
        assert await list_project_sessions("u1", PROJECT) == ([], None)

    @pytest.mark.asyncio
    async def test_missing_index_degrades_to_empty(self, sessions_metadata_table, monkeypatch):
        """Real DynamoDB's spelling of a missing GSI; the list must not 500 while it builds."""
        import apis.shared.sessions.metadata as md

        class _NoIndexTable:
            def query(self, **kwargs):
                raise ClientError(
                    {"Error": {
                        "Code": "ValidationException",
                        "Message": "The table does not have the specified index: ProjectSessionIndex",
                    }},
                    "Query",
                )

        monkeypatch.setattr(md, "get_dynamodb_table", lambda _name: _NoIndexTable())
        assert await md.list_project_sessions("u1", PROJECT) == ([], None)

    @pytest.mark.asyncio
    async def test_other_query_errors_propagate(self, sessions_metadata_table, monkeypatch):
        import apis.shared.sessions.metadata as md

        class _ThrottledTable:
            def query(self, **kwargs):
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "Query")

        monkeypatch.setattr(md, "get_dynamodb_table", lambda _name: _ThrottledTable())
        with pytest.raises(ClientError):
            await md.list_project_sessions("u1", PROJECT)
