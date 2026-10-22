"""The lexical leg, the row join, the token bucket and the merge, below the route."""

from __future__ import annotations

import pytest

from apis.shared.conversation_search.models import ConversationSearchResult
from apis.shared.conversation_search.service import TextSearchThrottle, merge_results
from apis.shared.conversation_search.session_rows import get_owned_session_rows, search_user_sessions_lexical
from apis.shared.sessions.search_text import first_prompt_value, normalize_search_text


def _put(table, user_id, session_id, title, *, status="active", first_prompt="", title_lower=None, project_id=None, sk=None):
    item = {
        "PK": f"USER#{user_id}",
        "SK": sk or f"S#{session_id}",
        "sessionId": session_id,
        "userId": user_id,
        "title": title,
        "status": status,
        "lastMessageAt": "2026-10-01T00:00:00+00:00",
    }
    lower = normalize_search_text(title) if title_lower is None else title_lower
    if lower:
        item["titleLower"] = lower
    if first_prompt:
        item["firstPrompt"] = first_prompt_value(first_prompt)
    if status == "deleted":
        item["deleted"] = True
    if project_id:
        item["preferences"] = {"projectId": project_id, "assistantId": "ast-1"}
        item["GSI5_PK"] = f"PROJECT#{project_id}#USER#{user_id}"
        item["GSI5_SK"] = f"2026-10-01T00:00:00+00:00#{session_id}"
    table.put_item(Item=item)


@pytest.mark.asyncio
async def test_matches_title_and_opening_prompt_case_insensitively(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "s1", "Window Functions in SQL")
    _put(sessions_metadata_table, "u1", "s2", "Untitled", first_prompt="Explain WINDOW functions please")
    _put(sessions_metadata_table, "u1", "s3", "Something else")
    rows = await search_user_sessions_lexical("u1", "window FUNCTIONS")
    assert sorted(r.session_id for r in rows) == ["s1", "s2"]


@pytest.mark.asyncio
async def test_only_the_callers_partition(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "s1", "Budget")
    _put(sessions_metadata_table, "u2", "s2", "Budget")
    assert [r.session_id for r in await search_user_sessions_lexical("u1", "budget")] == ["s1"]


@pytest.mark.asyncio
async def test_archived_included_deleted_and_legacy_rows_without_title_lower_skipped(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "live", "Budget live")
    _put(sessions_metadata_table, "u1", "arch", "Budget archived", status="archived")
    _put(sessions_metadata_table, "u1", "dead", "Budget deleted", status="deleted")
    _put(sessions_metadata_table, "u1", "old", "Budget before backfill", title_lower="")
    rows = {r.session_id: r for r in await search_user_sessions_lexical("u1", "budget")}
    assert set(rows) == {"live", "arch"}
    assert rows["arch"].archived and not rows["live"].archived


@pytest.mark.asyncio
async def test_non_session_rows_in_the_partition_are_ignored(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "s1", "Budget")
    sessions_metadata_table.put_item(Item={"PK": "USER#u1", "SK": "C#2026#x", "titleLower": "budget", "status": "active"})
    assert [r.session_id for r in await search_user_sessions_lexical("u1", "budget")] == ["s1"]


@pytest.mark.asyncio
async def test_scan_is_capped(sessions_metadata_table):
    for i in range(30):
        _put(sessions_metadata_table, "u1", f"s{i:02d}", f"Budget {i}")
    rows = await search_user_sessions_lexical("u1", "budget", scan_cap=10)
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_project_scope_reads_the_project_index(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "p1", "Budget", project_id="proj-1")
    _put(sessions_metadata_table, "u1", "p2", "Budget", project_id="proj-2")
    _put(sessions_metadata_table, "u1", "free", "Budget")
    rows = await search_user_sessions_lexical("u1", "budget", project_id="proj-1")
    assert [(r.session_id, r.project_id, r.assistant_id) for r in rows] == [("p1", "proj-1", "ast-1")]


@pytest.mark.asyncio
async def test_blank_query_reads_nothing(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "s1", "Budget")
    assert await search_user_sessions_lexical("u1", "   ") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["", None])
async def test_lexical_refuses_an_empty_user(sessions_metadata_table, user_id):
    with pytest.raises(ValueError):
        await search_user_sessions_lexical(user_id, "budget")


@pytest.mark.asyncio
async def test_join_finds_only_the_callers_live_rows(sessions_metadata_table):
    _put(sessions_metadata_table, "u1", "mine", "A")
    _put(sessions_metadata_table, "u1", "gone", "B", status="deleted")
    _put(sessions_metadata_table, "u2", "theirs", "C")
    rows = await get_owned_session_rows("u1", ["mine", "gone", "theirs", "missing", "mine"])
    assert set(rows) == {"mine"}


@pytest.mark.asyncio
async def test_join_of_nothing_reads_nothing(sessions_metadata_table):
    assert await get_owned_session_rows("u1", []) == {}


class TestThrottle:
    def test_bucket_refills_over_a_minute(self):
        now = [0.0]
        bucket = TextSearchThrottle(capacity=3, per_seconds=60, clock=lambda: now[0])
        assert [bucket.try_acquire("u") for _ in range(4)] == [True, True, True, False]
        now[0] = 20.0  # one token back
        assert bucket.try_acquire("u") is True
        assert bucket.try_acquire("u") is False

    def test_buckets_are_per_user_and_bounded(self):
        bucket = TextSearchThrottle(capacity=1, max_users=2, clock=lambda: 0.0)
        assert bucket.try_acquire("a") and not bucket.try_acquire("a")
        assert bucket.try_acquire("b")
        assert bucket.try_acquire("c")  # evicts "a", the least recently seen
        assert bucket.try_acquire("a")  # a fresh bucket


def _r(sid, kind, **kw):
    return ConversationSearchResult(session_id=sid, title=sid, last_message_at="t", match_kind=kind, **kw)


def test_merge_order_dedupe_and_limit():
    titles = [_r("t1", "title"), _r("both", "title")]
    texts = [_r("x1", "text", score=0.9, message_id="msg-x1-0"), _r("both", "text", score=0.8, message_id="msg-both-4", snippet="s", also_matched=["msg-both-6"])]
    prompts = [_r("p1", "prompt"), _r("x1", "prompt")]
    merged = merge_results(titles, texts, prompts, limit=10)
    assert [(r.session_id, r.match_kind) for r in merged] == [("t1", "title"), ("both", "title"), ("x1", "text"), ("p1", "prompt")]
    both = merged[1]
    assert (both.message_id, both.snippet, both.score, both.also_matched) == ("msg-both-4", "s", 0.8, ["msg-both-6"])
    assert len(merge_results(titles, texts, prompts, limit=2)) == 2
