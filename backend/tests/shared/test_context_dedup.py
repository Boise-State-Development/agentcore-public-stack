"""Unit tests for retrieval duplicate-content removal.

Issue: #1236 — managed-KB retrieval returns duplicate/overlapping chunks, fed to
the model and shown as citations. Deduplication lives above the backend seam in
``rag_service`` so it holds identically on every engine; these tests exercise the
pure helpers directly (no AWS, no DynamoDB).
"""

from apis.shared.assistants.rag_service import (
    _collapse_repeated_segments,
    _is_near_duplicate,
    _normalize_for_compare,
    dedupe_context_chunks,
)


def _chunk(text, doc_id="doc-a", key="k", distance=0.5):
    """A formatted result dict in the shape the facade returns."""
    return {
        "text": text,
        "distance": distance,
        "metadata": {"document_id": doc_id, "filename": f"{doc_id}.pdf"},
        "key": key,
    }


# ── intra-chunk sentence/line collapse (cause 1: vision repetition) ───────────


def test_collapse_drops_repeated_sentence_within_chunk():
    text = (
        "Agriculture is responsible for 75% of global deforestation. "
        "Agriculture is responsible for 75% of global deforestation. "
        "Forests store carbon and protect biodiversity."
    )
    result = _collapse_repeated_segments(text)
    assert result.count("Agriculture is responsible for 75% of global deforestation") == 1
    assert "Forests store carbon and protect biodiversity" in result


def test_collapse_drops_repeated_lines():
    text = (
        "The chart shows rising emissions across every sector measured.\n"
        "The chart shows rising emissions across every sector measured.\n"
        "Transport is the fastest-growing contributor of the group."
    )
    result = _collapse_repeated_segments(text)
    assert result.count("The chart shows rising emissions across every sector measured") == 1
    assert "Transport is the fastest-growing contributor" in result


def test_collapse_preserves_first_occurrence_order():
    text = "First distinct sentence here about topic. Second distinct sentence about it. First distinct sentence here about topic."
    result = _collapse_repeated_segments(text)
    assert result.index("First distinct") < result.index("Second distinct")
    assert result.count("First distinct sentence here about topic") == 1


def test_collapse_keeps_short_repeated_segments():
    # Short recurring lines (below the char threshold) are legitimate; not dropped.
    text = "Yes.\nYes.\nYes."
    assert _collapse_repeated_segments(text) == text


def test_collapse_noop_on_distinct_text():
    text = "Alpha sentence about one thing. Beta sentence about another thing entirely."
    assert _collapse_repeated_segments(text) == text


def test_collapse_handles_empty_and_blank():
    assert _collapse_repeated_segments("") == ""
    assert _collapse_repeated_segments("   ") == "   "


# ── cross-chunk near-duplicate detection ─────────────────────────────────────


def test_near_duplicate_exact_normalized_equality():
    a = _normalize_for_compare("The Same Text.")
    b = _normalize_for_compare("the same   text.")
    assert _is_near_duplicate(a, frozenset(a.split()), b, frozenset(b.split()))


def test_near_duplicate_substring_containment():
    small = _normalize_for_compare("carbon emissions rose sharply")
    big = _normalize_for_compare("in 2024 carbon emissions rose sharply across all sectors")
    assert _is_near_duplicate(small, frozenset(small.split()), big, frozenset(big.split()))


def test_short_distinct_chunks_not_fused():
    # Few shared words but genuinely different; fuzzy overlap must not fire.
    a = _normalize_for_compare("cats are mammals")
    b = _normalize_for_compare("dogs are mammals")
    assert not _is_near_duplicate(a, frozenset(a.split()), b, frozenset(b.split()))


def test_distinct_long_chunks_not_fused():
    a = _normalize_for_compare(
        "The report examines rainfall patterns across the northern basin over ten years of data."
    )
    b = _normalize_for_compare(
        "A separate appendix lists funding sources and the review committee members by name."
    )
    assert not _is_near_duplicate(a, frozenset(a.split()), b, frozenset(b.split()))


# ── dedupe_context_chunks: the wired pass ────────────────────────────────────


def test_dedupe_drops_exact_duplicate_chunk():
    chunks = [
        _chunk("Renewable capacity doubled between 2019 and 2024 worldwide.", key="k1"),
        _chunk("Renewable capacity doubled between 2019 and 2024 worldwide.", key="k2"),
        _chunk("Grid storage remains the primary bottleneck for further growth.", key="k3"),
    ]
    result = dedupe_context_chunks(chunks)
    assert len(result) == 2
    assert result[0]["key"] == "k1"  # best-first: first occurrence kept
    assert result[1]["key"] == "k3"


def test_dedupe_collapses_within_then_drops_across():
    repeated = (
        "Solar prices fell by ninety percent over the past decade of deployment. "
        "Solar prices fell by ninety percent over the past decade of deployment."
    )
    chunks = [
        _chunk(repeated, key="k1"),
        _chunk("Solar prices fell by ninety percent over the past decade of deployment.", key="k2"),
        _chunk("Wind now supplies a fifth of the region's electricity demand.", key="k3"),
    ]
    result = dedupe_context_chunks(chunks)
    # k1's internal repeat collapses, and k2 (identical to the collapsed k1) drops.
    assert len(result) == 2
    assert result[0]["key"] == "k1"
    assert result[0]["text"].count("Solar prices fell by ninety percent") == 1
    assert result[1]["key"] == "k3"


def test_dedupe_keeps_distinct_chunks_and_metadata():
    chunks = [
        _chunk("Chapter one covers the history of the watershed and its settlement.", doc_id="d1", key="k1"),
        _chunk("Chapter two details modern water-rights disputes among the counties.", doc_id="d2", key="k2"),
    ]
    result = dedupe_context_chunks(chunks)
    assert len(result) == 2
    assert result[0]["metadata"]["document_id"] == "d1"
    assert result[1]["metadata"]["filename"] == "d2.pdf"
    assert result[0]["distance"] == 0.5


def test_dedupe_empty_list():
    assert dedupe_context_chunks([]) == []


def test_dedupe_does_not_mutate_input():
    original = _chunk(
        "Repeated line about the survey results.\nRepeated line about the survey results.",
        key="k1",
    )
    snapshot = original["text"]
    dedupe_context_chunks([original])
    assert original["text"] == snapshot  # original dict untouched
