"""Prompt-cache classification of api-converse cost rows.

Every API-key request is recorded under one ``api-converse-<key>`` pseudo-
session, so a batch client's unrelated requests sit side by side in one
"session". Without prefix fingerprints the derivation chained each request to
whatever ran before it, and every zero-read call inside the 30-minute OpenAI
Responses TTL was reported as ``miss_avoidable`` — the alarm meant for chat
prefix-ordering regressions fired on batch traffic it can say nothing about.

These tests run the real ``_record_cost`` (only the store is stubbed, to
capture the row) and then classify the captured row with the real
``_derive_cache_observability`` against the earlier captured rows.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.chat import converse_routes
from apis.app_api.chat.models import ConverseMessage, ConverseRequest
from apis.shared.sessions import metadata as md


pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
MODEL_ID = "global.openai.gpt-6-sol"
PROVIDER = "bedrock-responses"  # 30-minute TTL

PRICING = {
    "inputPricePerMtok": 2.0,
    "outputPricePerMtok": 10.0,
    "cacheWritePricePerMtok": 2.5,
    "cacheReadPricePerMtok": 0.2,
    "currency": "USD",
    "snapshotAt": "2026-10-14T00:00:00Z",
}


def _request(system, *turns):
    """A request whose messages alternate user/assistant, starting with user."""
    roles = ["user", "assistant"]
    return ConverseRequest(
        model_id=MODEL_ID,
        system_prompt=system,
        messages=[ConverseMessage(role=roles[i % 2], content=t) for i, t in enumerate(turns)],
    )


def _usage(cache_read=0, cache_write=4000):
    return {
        "inputTokens": 500,
        "outputTokens": 200,
        "cacheReadInputTokens": cache_read,
        "cacheWriteInputTokens": cache_write,
    }


async def _record(request, usage, pricing=PRICING):
    """Run the real _record_cost and return the MessageMetadata it stored."""
    store = AsyncMock()
    with patch.object(converse_routes, "store_message_metadata", store), patch.object(
        converse_routes, "create_pricing_snapshot", AsyncMock(return_value=pricing)
    ):
        await converse_routes._record_cost(
            user_id="user-a",
            model_id=MODEL_ID,
            usage=usage,
            key_id="key-a",
            request=request,
            provider=PROVIDER,
        )
    store.assert_awaited_once()
    return store.await_args.kwargs["message_metadata"]


def _as_row(meta, seconds_ago):
    """The stored C# row, as the derivation reads it back from the GSI."""
    row = meta.model_dump(by_alias=True, exclude_none=True)
    row["timestamp"] = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    return row


class _FakeTable:
    def __init__(self, items):
        self._items = items

    def query(self, **kwargs):
        return {"Items": self._items}


def _classify(meta, earlier_rows):
    return md._derive_cache_observability(
        session_id="api-converse-key-a",
        table=_FakeTable(earlier_rows),
        timestamp=NOW.isoformat(),
        message_metadata=meta,
    )


class TestRowCarriesFingerprintsAndPricing:
    async def test_fingerprints_use_the_runtime_field_names(self):
        meta = await _record(_request("sys", "hello"), _usage())
        prints = meta.prefixFingerprints
        for field in ("toolConfigHash", "systemPromptHash", "historyHash", "messageCount"):
            assert field in prints
        assert prints["messageCount"] == 1

    async def test_system_prompt_changes_the_system_hash_only(self):
        a = (await _record(_request("sys A", "hello"), _usage())).prefixFingerprints
        b = (await _record(_request("sys B", "hello"), _usage())).prefixFingerprints
        assert a["systemPromptHash"] != b["systemPromptHash"]
        assert a["toolConfigHash"] == b["toolConfigHash"]
        assert a["historyHash"] == b["historyHash"]

    async def test_pricing_snapshot_is_on_the_row(self):
        meta = await _record(_request("sys", "hello"), _usage())
        snapshot = md._extract_pricing_dict(meta)
        assert snapshot["cacheWritePricePerMtok"] == 2.5
        assert snapshot["cacheReadPricePerMtok"] == 0.2

    async def test_a_snapshot_that_does_not_validate_still_records_the_row(self):
        meta = await _record(
            _request("sys", "hello"), _usage(), pricing={"inputPricePerMtok": 2.0, "outputPricePerMtok": 10.0}
        )
        assert meta.model_info.pricing_snapshot is None
        assert meta.cost is not None


class TestUnrelatedRequestsAreNotAvoidableMisses:
    async def test_different_system_prompts_inside_the_ttl(self):
        first = await _record(_request("Classify the ticket.", "ticket 1"), _usage())
        second = await _record(_request("Summarize the ticket.", "ticket 1"), _usage())

        result = _classify(second, [_as_row(first, seconds_ago=4)])

        assert result["cacheStatus"] != "miss_avoidable"
        assert result["cacheStatus"] == "first_write"
        assert result["wastedUsd"] == 0.0

    async def test_same_system_prompt_different_items_inside_the_ttl(self):
        # The batch shape behind the prod alarm: one instruction, a new item
        # per request. Only the system prompt is shared, and nothing says it
        # was long enough to cache, so the zero read is not evidence of waste.
        first = await _record(_request("Classify the ticket.", "ticket 1"), _usage())
        second = await _record(_request("Classify the ticket.", "ticket 2"), _usage())

        result = _classify(second, [_as_row(first, seconds_ago=4)])

        assert result["cacheStatus"] == "first_write"
        assert result["wastedUsd"] == 0.0

    async def test_a_full_window_of_batch_rows_never_chains(self):
        rows = []
        for i in range(10):
            meta = await _record(_request("Classify the ticket.", f"ticket {i}"), _usage())
            rows.insert(0, _as_row(meta, seconds_ago=4 * (10 - i)))
        newest = await _record(_request("Classify the ticket.", "ticket 10"), _usage())

        assert _classify(newest, rows)["cacheStatus"] == "first_write"


class TestSamePrefixZeroReadIsStillCaught:
    async def test_identical_resend_inside_the_ttl_is_avoidable_and_priced(self):
        first = await _record(_request("Classify the ticket.", "ticket 1"), _usage(cache_write=4000))
        second = await _record(_request("Classify the ticket.", "ticket 1"), _usage(cache_write=4000))

        result = _classify(second, [_as_row(first, seconds_ago=4)])

        assert result["cacheStatus"] == "miss_avoidable"
        # min(4000 written, 4000 previously cached) at the 2.5 - 0.2 premium.
        assert result["wastedUsd"] == pytest.approx((4000 / 1_000_000) * 2.3)
        assert result["wastedUsd"] > 0

    async def test_next_turn_of_a_client_held_conversation_is_avoidable(self):
        first = await _record(_request("You are helpful.", "q1"), _usage(cache_write=3000))
        second = await _record(
            _request("You are helpful.", "q1", "a1", "q2"), _usage(cache_write=3500)
        )

        result = _classify(second, [_as_row(first, seconds_ago=60)])

        assert result["cacheStatus"] == "miss_avoidable"
        assert result["wastedUsd"] == pytest.approx((3000 / 1_000_000) * 2.3)

    async def test_the_continuation_reading_its_prefix_is_a_hit(self):
        first = await _record(_request("You are helpful.", "q1"), _usage(cache_write=3000))
        second = await _record(
            _request("You are helpful.", "q1", "a1", "q2"), _usage(cache_read=3000, cache_write=500)
        )

        result = _classify(second, [_as_row(first, seconds_ago=60)])

        assert result["cacheStatus"] == "hit"

    async def test_the_predecessor_is_found_behind_unrelated_rows(self):
        convo = await _record(_request("You are helpful.", "q1"), _usage(cache_write=3000))
        noise = await _record(_request("Classify the ticket.", "ticket 1"), _usage())
        resend = await _record(_request("You are helpful.", "q1"), _usage(cache_write=3000))

        result = _classify(resend, [_as_row(noise, seconds_ago=4), _as_row(convo, seconds_ago=8)])

        assert result["cacheStatus"] == "miss_avoidable"
        assert result["cacheGapSeconds"] == 4
        assert result["cachePrefixGapSeconds"] == 8

    async def test_a_resend_past_the_ttl_is_expired_not_avoidable(self):
        first = await _record(_request("Classify the ticket.", "ticket 1"), _usage())
        second = await _record(_request("Classify the ticket.", "ticket 1"), _usage())

        result = _classify(second, [_as_row(first, seconds_ago=1801)])

        assert result["cacheStatus"] == "miss_ttl_expired"
        assert result["wastedUsd"] == 0.0


class TestLegacyRows:
    async def test_rows_written_before_fingerprints_never_match(self):
        # Rows already in the pseudo-session carry no prefixFingerprints. The
        # first fingerprinted call after deploy must not chain to them.
        new = await _record(_request("Classify the ticket.", "ticket 1"), _usage())
        legacy = {
            "timestamp": (NOW - timedelta(seconds=4)).isoformat(),
            "tokenUsage": {"cacheReadInputTokens": 0, "cacheWriteInputTokens": 4000},
        }

        assert _classify(new, [legacy])["cacheStatus"] == "first_write"
