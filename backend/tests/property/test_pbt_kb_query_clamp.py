"""Property-based tests for the retrieval query clamp.

Feature: managed-kb-migration

**Property 3: the clamp is total and non-throwing.**

Managed Knowledge Base rejects a ``Retrieve`` query over 10,000 characters
outright, and the quota is not adjustable. So the clamp sits on a request path
where the only acceptable behaviours are "shortened" or "unchanged" — never
"raised". A clamp that threw would convert a fixable input into a failed chat
turn, which is strictly worse than answering a slightly truncated question.

"Total" is the load-bearing word: *every* input must map to an output, including
the awkward ones. The strategies below deliberately include empty strings, strings
made entirely of astral-plane characters, and lengths sitting exactly on the
boundary, because those are where a length check written against the wrong unit or
with an off-by-one starts returning 10,001 characters to an API that rejects
10,001 characters.

Validates: Requirements 4.1, 4.3, 4.4.
"""

from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies as st

from apis.shared.assistants.kb_access import granted
from apis.shared.kb_backend.query_guard import MAX_QUERY_CHARS, clamp_query

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Any text at all, including empty and including characters that are one code
#: point but more than one byte — the clamp counts characters, and a byte-based
#: implementation would pass a naive ASCII-only test.
st_any_text = st.text(max_size=200)

st_long_text = st.text(min_size=1, max_size=50).map(
    lambda s: s * (MAX_QUERY_CHARS // max(len(s), 1) + 2)
)

st_multibyte_text = st.text(
    alphabet=st.characters(min_codepoint=0x1F300, max_codepoint=0x1F5FF),
    min_size=1,
    max_size=40,
).map(lambda s: s * (MAX_QUERY_CHARS // max(len(s), 1) + 2))

#: Lengths straddling the cap, where off-by-one errors live.
st_boundary_length = st.integers(
    min_value=MAX_QUERY_CHARS - 2, max_value=MAX_QUERY_CHARS + 2
)


# ---------------------------------------------------------------------------
# Totality and the cap
# ---------------------------------------------------------------------------
@given(query=st_any_text)
@settings(max_examples=200)
def test_short_queries_pass_through_unchanged(query):
    """Below the cap the clamp must be the identity, not a normalizer.

    Anything else would silently change what users are asking.
    """
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        result, truncated = clamp_query(query)
    assert result == query
    assert truncated is False


@given(query=st.one_of(st_long_text, st_multibyte_text))
@settings(max_examples=100)
def test_output_never_exceeds_the_cap(query):
    """The whole point: the value handed to the backend always fits."""
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        result, truncated = clamp_query(query)
    assert len(result) <= MAX_QUERY_CHARS
    assert truncated is True


@given(length=st_boundary_length)
@settings(max_examples=50)
def test_the_boundary_is_inclusive(length):
    """Exactly MAX_QUERY_CHARS is allowed; one more is not.

    Managed KB accepts 10,000 and rejects 10,001, so an off-by-one here is a
    request error rather than a shorter answer.
    """
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        result, truncated = clamp_query("x" * length)

    assert len(result) == min(length, MAX_QUERY_CHARS)
    assert truncated == (length > MAX_QUERY_CHARS)


@given(query=st.one_of(st_any_text, st_long_text, st_multibyte_text))
@settings(max_examples=200)
def test_the_clamp_never_raises(query):
    """Totality. A raise here would turn a long question into a failed chat turn."""
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        try:
            clamp_query(query)
        except Exception as exc:  # pragma: no cover - the assertion is the point
            pytest.fail(f"clamp_query raised {type(exc).__name__}: {exc}")


@given(query=st_long_text)
@settings(max_examples=50)
def test_truncation_keeps_the_head(query):
    """Keep the beginning: for a natural-language query that is where the intent
    is. Head-truncating would change the question rather than shorten it."""
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        result, _ = clamp_query(query)
    assert query.startswith(result)


@given(query=st_long_text)
@settings(max_examples=50)
def test_the_clamp_is_idempotent(query):
    """Clamping twice equals clamping once, and the second pass reports no
    truncation — so a retry does not double-count the metric."""
    with patch("apis.shared.kb_backend.query_guard.emit_count"):
        once, first = clamp_query(query)
        twice, second = clamp_query(once)
    assert twice == once
    assert first is True
    assert second is False


# ---------------------------------------------------------------------------
# The truncation signal
# ---------------------------------------------------------------------------
@given(length=st_boundary_length)
@settings(max_examples=50)
def test_the_metric_is_emitted_exactly_when_truncation_happened(length):
    """The signal must track reality in both directions.

    A metric that over-reports trains operators to ignore it; one that
    under-reports hides the fact that users are already sending queries the
    managed backend would reject.
    """
    with patch("apis.shared.kb_backend.query_guard.emit_count") as emit:
        _, truncated = clamp_query("x" * length)

    assert truncated == (length > MAX_QUERY_CHARS)
    assert emit.called == truncated


def test_a_metric_failure_does_not_break_the_clamp():
    """Observability is never control flow: if CloudWatch is down the query still
    gets clamped and the search still runs."""
    with patch(
        "apis.shared.kb_backend.query_guard.emit_count",
        side_effect=RuntimeError("cloudwatch unavailable"),
    ):
        with pytest.raises(RuntimeError):
            # Confirms the patch is actually wired, so the next assertion is not
            # vacuous.
            clamp_query("x" * (MAX_QUERY_CHARS + 1))

    # emit_count's real implementation swallows its own failures, which is what
    # makes the above impossible in production. Assert that contract directly.
    from apis.shared.kb_backend.metrics import emit_count

    with patch("boto3.client", side_effect=RuntimeError("no credentials")):
        emit_count("KbQueryClamped")  # must not raise


@pytest.mark.parametrize("falsy", ["", None])
def test_empty_input_is_handled_without_a_metric(falsy):
    """An empty query is not a truncation."""
    with patch("apis.shared.kb_backend.query_guard.emit_count") as emit:
        result, truncated = clamp_query(falsy)
    assert result == ""
    assert truncated is False
    emit.assert_not_called()


# ---------------------------------------------------------------------------
# Guards the properties above cannot provide
#
# Every test above refers to MAX_QUERY_CHARS symbolically, so all of them follow
# the constant wherever it goes — raise it to 32,000 and they all still pass while
# the managed backend starts rejecting requests. These three assertions were added
# after mutation testing showed exactly that: three separate mutations survived a
# suite that looked thorough.
# ---------------------------------------------------------------------------
def test_the_cap_is_the_literal_managed_kb_limit():
    """Pinned to 10,000 as a LITERAL, not to the constant.

    This is the one assertion in the file that cannot be satisfied by moving the
    constant. 10,000 is Managed KB's `Retrieve` input quota and it is not
    adjustable, so this number is a property of AWS, not a tuning knob. Raising it
    does not buy longer queries; it buys rejected requests.
    """
    assert MAX_QUERY_CHARS == 10_000


@pytest.mark.asyncio
async def test_the_facade_actually_clamps_before_dispatch():
    """The clamp must be WIRED, not merely correct.

    Nothing else in this file would notice if the facade stopped calling
    clamp_query: the unit-level properties would all still pass while every long
    query went to the backend intact. Asserted by inspecting what the backend
    actually received.
    """
    from apis.shared.assistants import rag_service

    seen = {}

    class _RecordingBackend:
        async def search(self, kb_ref, query, top_k=5):
            seen["query"] = query
            return []

    with patch.object(rag_service, "resolve_backend", return_value=_RecordingBackend()), patch.object(
        rag_service, "emit_count"
    ), patch("apis.shared.kb_backend.query_guard.emit_count"):
        await rag_service.search_assistant_knowledgebase_with_formatting(
            "ast-1",
            "x" * (MAX_QUERY_CHARS + 500),
            access=granted("ast-1", "user-clamp", "owner"),
        )

    assert seen["query"] is not None
    assert len(seen["query"]) == MAX_QUERY_CHARS, (
        "the facade dispatched an unclamped query; the clamp is dead code"
    )


def test_the_metric_namespace_is_not_a_reserved_aws_one():
    """CloudWatch rejects PutMetricData into any namespace beginning with "AWS".

    A reserved namespace would make every publish silently denied — the grant looks
    correct, the code looks correct, and no metric ever arrives. The CDK grant
    conditions on this same namespace, so the two must agree; this is the backend
    half of that assertion.
    """
    from apis.shared.kb_backend.metrics import metric_namespace

    ns = metric_namespace()
    assert not ns.startswith("AWS"), f"{ns!r} is a reserved namespace; writes are rejected"
    assert ns.endswith("/ManagedKb")
