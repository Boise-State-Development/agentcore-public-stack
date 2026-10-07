"""The text helpers conversation search's writer and reader share.

The lexical leg is a case-sensitive DynamoDB ``contains()``, so the one thing
that must hold is that the stored attribute and the compared query come out of
the same normalizer. The rest covers the snippet window and the timestamp the
index hands back reformatted.
"""

from datetime import datetime, timezone

import pytest

from apis.shared.sessions.search_text import (
    FIRST_PROMPT_MAX_CHARS,
    SNIPPET_MAX_CHARS,
    first_prompt_value,
    make_snippet,
    normalize_search_text,
    parse_timestamp,
    query_terms,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Window Function", "window function"),
            ("  BIO   101\n", "bio 101"),
            ("Ｗindow", "window"),  # full-width folds to ASCII (NFKC)
            ("ﬁle", "file"),  # ligature
            ("Straße", "strasse"),  # casefold, not lower
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize_search_text(raw) == expected

    def test_query_and_stored_title_meet(self):
        stored = normalize_search_text("Q3 Budget — Window Functions")
        assert normalize_search_text("WINDOW  function") in stored

    def test_first_prompt_is_normalized_then_cut(self):
        value = first_prompt_value("Hello   World " + "x" * 400)
        assert value.startswith("hello world x")
        assert len(value) == FIRST_PROMPT_MAX_CHARS

    def test_first_prompt_of_nothing_is_empty(self):
        assert first_prompt_value(None) == ""
        assert first_prompt_value("   ") == ""

    def test_query_terms_longest_first_and_short_words_dropped(self):
        assert query_terms("a window to SQL") == ["window", "sql", "to"]


class TestSnippet:
    def test_short_text_is_returned_whole(self):
        assert make_snippet("short   answer\n\nhere", "answer") == "short answer here"

    def test_window_lands_on_the_phrase(self):
        text = "lead " * 120 + "the Window Function answer " + "tail " * 120
        snippet = make_snippet(text, "window function")
        assert "Window Function" in snippet
        assert snippet.startswith("…") and snippet.endswith("…")
        assert len(snippet) <= SNIPPET_MAX_CHARS

    def test_falls_back_to_a_word_of_the_query(self):
        text = "lead " * 120 + "partition by region " + "tail " * 120
        assert "partition" in make_snippet(text, "how do I partition rows")

    def test_semantic_match_shows_the_opening(self):
        text = "Opening line. " + "z" * 600
        snippet = make_snippet(text, "nothing literal")
        assert snippet.startswith("Opening line.")
        assert snippet.endswith("…")
        assert len(snippet) == SNIPPET_MAX_CHARS


class TestParseTimestamp:
    def test_bedrock_reformatted_value(self):
        # Stored 2026-10-07T17:09:45.716213+00:00; Bedrock returned this (dev, 2026-10-07).
        assert parse_timestamp("2026-10-07T17:09:45.716Z") == datetime(2026, 10, 7, 17, 9, 45, 716000, tzinfo=timezone.utc)

    def test_stored_value(self):
        assert parse_timestamp("2026-10-07T17:09:45.716213+00:00") == datetime(
            2026, 10, 7, 17, 9, 45, 716213, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("value", ["2026-10-07T17:09:45.7Z", "2026-10-07T17:09:45.123456789Z", "2026-10-07T17:09:45"])
    def test_other_precisions_and_naive(self, value):
        parsed = parse_timestamp(value)
        assert parsed is not None and parsed.tzinfo is not None

    @pytest.mark.parametrize("value", [None, "", "not a date", 12345])
    def test_unparseable_is_none(self, value):
        assert parse_timestamp(value) is None
