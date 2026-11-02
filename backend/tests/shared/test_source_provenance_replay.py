"""``GET /messages`` replay of source-provenance verdicts.

The ``source_provenance`` SSE event is emitted once and never re-streams, so
the replay is what a reloaded conversation shows. A ``quote_unsupported`` that
vanishes on refresh reads as a clean answer, which is worse than no badge.

These are contract tests on the response shape and on how the fetch is gated.
The query itself is covered by ``test_source_provenance_store.py``, and the
round trip through Memory is a dev check (the spec's §12).
"""

from __future__ import annotations

import inspect

from apis.shared.sessions import messages as messages_mod
from apis.shared.sessions.models import MessagesListResponse


def _row():
    return {
        "toolUseId": "tu-1",
        "claims": [
            {
                "id": "total",
                "state": "quote_unsupported",
                "method": "no_match",
                "quote": "The total approved budget is $487,500",
                "locator": {"kind": "page", "value": 3, "precision": "measured"},
                "uploadId": "up-1",
                "filename": "award.pdf",
            }
        ],
    }


def test_the_response_carries_source_provenance_under_its_spa_alias():
    body = MessagesListResponse(messages=[], source_provenance=[_row()]).model_dump(by_alias=True)
    assert body["sourceProvenance"] == [_row()]


def test_it_defaults_to_empty_so_an_old_client_and_a_quiet_turn_look_alike():
    body = MessagesListResponse(messages=[]).model_dump(by_alias=True)
    assert body["sourceProvenance"] == []


def test_a_replayed_claim_keeps_its_precision():
    """The SPA hedges an estimated locator, so losing the flag on reload would
    turn a hedged citation into a confident one."""
    row = _row()
    row["claims"][0]["locator"]["precision"] = "estimated"
    body = MessagesListResponse(messages=[], source_provenance=[row]).model_dump(by_alias=True)
    assert body["sourceProvenance"][0]["claims"][0]["locator"]["precision"] == "estimated"


def test_the_fetch_is_flag_gated_and_reads_the_store_off_the_event_loop():
    source = inspect.getsource(messages_mod)
    fetch = source[source.index("async def fetch_source_provenance") :]
    fetch = fetch[: fetch.index("# Run fetches in parallel")]
    # Off, no query is made at all: a deployment without the feature pays
    # nothing on every page load.
    assert "source_provenance_enabled()" in fetch
    assert "return []" in fetch
    # The store is sync boto3; a direct call would block the event loop.
    assert "asyncio.to_thread" in fetch
    assert "get_source_provenance_store" in fetch


def test_the_replay_is_first_page_only():
    """Same reasoning as toolSummaries: the SPA keys verdicts by toolUseId and
    holds them all regardless of which page renders the correlated block."""
    source = inspect.getsource(messages_mod)
    block = source[source.index("source_provenance: List[Dict[str, Any]] = []") :]
    block = block[: block.index("return MessagesListResponse")]
    assert "if not next_token:" in block
