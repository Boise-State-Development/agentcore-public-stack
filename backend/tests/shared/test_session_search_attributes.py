"""``titleLower`` and ``firstPrompt``: the session-row attributes the lexical leg reads.

Written beside the title on the same write (spec §5), so they can never be a
round trip of their own, and kept in step with every way a title changes.
"""

import pytest

from apis.shared.sessions.search_text import FIRST_PROMPT_MAX_CHARS


def _row(table, user_id="u1", session_id="s1"):
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"}).get("Item")


@pytest.mark.asyncio
async def test_title_write_carries_title_lower_and_first_prompt(sessions_metadata_table):
    from apis.shared.sessions.metadata import ensure_session_metadata_exists, update_session_title

    await ensure_session_metadata_exists("s1", "u1")
    await update_session_title("s1", "u1", "Window Functions", first_prompt="How do I use a WINDOW function?" + " x" * 400)
    row = _row(sessions_metadata_table)
    assert row["title"] == "Window Functions"
    assert row["titleLower"] == "window functions"
    assert row["firstPrompt"].startswith("how do i use a window function?")
    assert len(row["firstPrompt"]) <= FIRST_PROMPT_MAX_CHARS


@pytest.mark.asyncio
async def test_first_prompt_is_never_replaced_by_a_later_title(sessions_metadata_table):
    from apis.shared.sessions.metadata import ensure_session_metadata_exists, update_session_title

    await ensure_session_metadata_exists("s1", "u1")
    await update_session_title("s1", "u1", "First", first_prompt="the opening prompt")
    await update_session_title("s1", "u1", "Second", first_prompt="a later prompt")
    await update_session_title("s1", "u1", "Third")
    row = _row(sessions_metadata_table)
    assert (row["titleLower"], row["firstPrompt"]) == ("third", "the opening prompt")


@pytest.mark.asyncio
async def test_title_write_without_a_prompt_writes_no_first_prompt(sessions_metadata_table):
    from apis.shared.sessions.metadata import ensure_session_metadata_exists, update_session_title

    await ensure_session_metadata_exists("s1", "u1")
    await update_session_title("s1", "u1", "Named by the caller")
    row = _row(sessions_metadata_table)
    assert row["titleLower"] == "named by the caller"
    assert "firstPrompt" not in row


@pytest.mark.asyncio
async def test_full_row_store_keeps_title_lower_in_step(sessions_metadata_table):
    """The rename route writes through store_session_metadata."""
    from apis.shared.sessions.metadata import (
        ensure_session_metadata_exists,
        get_session_metadata,
        store_session_metadata,
        update_session_title,
    )

    await ensure_session_metadata_exists("s1", "u1")
    await update_session_title("s1", "u1", "Old Name", first_prompt="opening")
    meta = await get_session_metadata("s1", "u1")
    meta.title = "Renamed Budget Review"
    await store_session_metadata("s1", "u1", meta)
    row = _row(sessions_metadata_table)
    assert row["titleLower"] == "renamed budget review"
    assert row["firstPrompt"] == "opening"


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("false", False), ("yes", False), ("true", True), (" TRUE ", True)],
)
def test_conversation_search_is_on_only_when_explicitly_enabled(monkeypatch, value, expected):
    from apis.shared.feature_flags import conversation_search_enabled

    if value is None:
        monkeypatch.delenv("CONVERSATION_SEARCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CONVERSATION_SEARCH_ENABLED", value)
    assert conversation_search_enabled() is expected
