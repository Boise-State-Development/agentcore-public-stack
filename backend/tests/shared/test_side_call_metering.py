"""Cost rows for the task-model side calls (``apis.shared.costs.side_call_metering``).

Titles, tool-batch summaries and compaction summaries each write one ``C#`` row with a
``callKind``, priced from the catalog row of the model that ran, in the background, and
kept out of the prompt-cache classifier.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from apis.shared.costs import side_call_metering as scm
from apis.shared.models.models import ManagedModel
from apis.shared.models.task_models import SOURCE_ASSIGNED, SOURCE_DEFAULT, TaskModelChoice, TaskModelSlot
from apis.shared.sessions import ledger_rollups
from apis.shared.sessions import metadata as md
from apis.shared.sessions.models import MessageMetadata, ModelInfo, TokenUsage

MICRO = "us.amazon.nova-micro-v1:0"
SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
RESPONSE = {
    "usage": {"inputTokens": 400, "outputTokens": 12, "totalTokens": 412},
    "metrics": {"latencyMs": 380},
}


def _sonnet_row() -> ManagedModel:
    return ManagedModel.model_validate(
        {
            "id": "m-sonnet",
            "modelId": SONNET,
            "modelName": "Claude Sonnet 4.5",
            "provider": "bedrock",
            "providerName": "Anthropic",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "maxInputTokens": 200000,
            "maxOutputTokens": 64000,
            "inputPricePerMillionTokens": 3.0,
            "outputPricePerMillionTokens": 15.0,
            "cacheWritePricePerMillionTokens": 3.75,
            "cacheReadPricePerMillionTokens": 0.30,
            "enabled": True,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
    )


def _assigned_sonnet() -> TaskModelChoice:
    return TaskModelChoice(TaskModelSlot.TOOL_SUMMARY, SONNET, "low", SOURCE_ASSIGNED, _sonnet_row())


def _uncataloged_default() -> TaskModelChoice:
    return TaskModelChoice(TaskModelSlot.TOOL_SUMMARY, MICRO, None, SOURCE_DEFAULT, None)


@pytest.fixture
def stored(monkeypatch) -> List[Dict[str, Any]]:
    """Capture what reaches ``store_message_metadata`` instead of writing it."""
    calls: List[Dict[str, Any]] = []

    async def fake_store(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(md, "store_message_metadata", fake_store)
    return calls


@pytest.fixture
def unmetered(monkeypatch) -> List[tuple]:
    calls: List[tuple] = []
    from apis.shared.observability import emf

    monkeypatch.setattr(emf, "emit_unmetered_model_call", lambda *a, **k: calls.append((a, k)))
    return calls


class TestRecord:
    @pytest.mark.asyncio
    async def test_an_assigned_model_is_priced_from_its_catalog_row(self, stored, unmetered):
        await scm.record_side_call_cost(
            call_kind=scm.TOOL_SUMMARY,
            session_id="s1",
            user_id="u1",
            choice=_assigned_sonnet(),
            usage=scm._usage_of(RESPONSE),
            latency_ms=380,
        )
        assert len(stored) == 1
        call = stored[0]
        assert call["message_id"] is None
        meta = call["message_metadata"]
        assert meta.model_extra["callKind"] == "tool_summary"
        assert meta.model_info.model_id == SONNET
        assert meta.model_info.pricing_snapshot.input_price_per_mtok == 3.0
        # 400 in at $3 + 12 out at $15, per million.
        assert meta.cost["total"] == pytest.approx((400 * 3.0 + 12 * 15.0) / 1_000_000)
        assert meta.latency.end_to_end_latency == 380
        assert unmetered == []

    @pytest.mark.asyncio
    async def test_an_uncataloged_default_writes_tokens_without_a_cost_or_an_alarm(self, stored, unmetered):
        with patch("apis.shared.costs.pricing_config.get_model_by_model_id") as lookup:
            await scm.record_side_call_cost(
                call_kind=scm.SESSION_TITLE,
                session_id="s1",
                user_id="u1",
                choice=_uncataloged_default(),
                usage=scm._usage_of(RESPONSE),
            )
        lookup.assert_not_called()  # the resolver already read the catalog and found no row
        meta = stored[0]["message_metadata"]
        assert meta.cost is None
        assert meta.token_usage.input_tokens == 400
        assert meta.model_extra["callKind"] == "session_title"
        assert unmetered == []

    @pytest.mark.asyncio
    async def test_an_assigned_model_with_no_price_fires_the_alarm(self, stored, unmetered, monkeypatch):
        """Resolved during a catalog outage (no record), and the row is still missing."""
        from apis.shared.costs import pricing_config

        async def no_row(_model_id):
            return None

        monkeypatch.setattr(pricing_config, "get_model_by_model_id", no_row)
        choice = TaskModelChoice(TaskModelSlot.COMPACTION_SUMMARY, SONNET, None, SOURCE_ASSIGNED, None)
        await scm.record_side_call_cost(
            call_kind=scm.COMPACTION_SUMMARY, session_id="s1", user_id="u1", choice=choice,
            usage=scm._usage_of(RESPONSE),
        )
        assert stored[0]["message_metadata"].cost is None
        assert len(unmetered) == 1
        args, kwargs = unmetered[0]
        assert args == (SONNET, "no_pricing")
        assert kwargs["surface"] == "compaction_summary"

    @pytest.mark.asyncio
    async def test_a_failed_write_never_raises(self, monkeypatch):
        async def boom(**_kwargs):
            raise RuntimeError("table gone")

        monkeypatch.setattr(md, "store_message_metadata", boom)
        await scm.record_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=_assigned_sonnet(),
            usage=scm._usage_of(RESPONSE),
        )


class TestSchedule:
    @pytest.mark.asyncio
    async def test_schedules_one_background_write(self, stored):
        scm.schedule_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=_assigned_sonnet(), response=RESPONSE
        )
        assert len(scm._pending) == 1
        await asyncio.gather(*list(scm._pending))
        assert len(stored) == 1
        assert not scm._pending

    @pytest.mark.parametrize(
        "session_id, user_id, response",
        [
            (None, "u1", RESPONSE),
            ("s1", None, RESPONSE),
            ("s1", "u1", {}),
            ("s1", "u1", {"usage": {"inputTokens": 0, "outputTokens": 0}}),
        ],
    )
    @pytest.mark.asyncio
    async def test_nothing_to_meter_schedules_nothing(self, stored, session_id, user_id, response):
        scm.schedule_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id=session_id, user_id=user_id,
            choice=_assigned_sonnet(), response=response,
        )
        assert not scm._pending
        assert stored == []

    def test_without_a_running_loop_it_does_nothing(self):
        scm.schedule_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=_assigned_sonnet(), response=RESPONSE
        )
        assert not scm._pending


class TestWorkerLoop:
    def test_rollups_spawned_by_the_store_finish_before_its_loop_closes(self, monkeypatch):
        """``_update_cost_summary_async`` fires the system and ledger rollups with
        ``create_task``. ``asyncio.run`` would cancel them on return; the worker waits."""
        landed: List[str] = []

        async def rollup():
            await asyncio.sleep(0.01)
            landed.append("rollup")

        async def fake_store(**_kwargs):
            asyncio.get_running_loop().create_task(rollup())
            return {}

        monkeypatch.setattr(md, "store_message_metadata", fake_store)
        scm._store_in_worker("s1", "u1", object())
        assert landed == ["rollup"]

    @pytest.mark.asyncio
    async def test_the_write_runs_off_the_calling_loop(self, monkeypatch):
        seen: Dict[str, Any] = {}
        caller = asyncio.get_running_loop()

        async def fake_store(**_kwargs):
            seen["loop"] = asyncio.get_running_loop()
            return {}

        monkeypatch.setattr(md, "store_message_metadata", fake_store)
        await scm.record_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=_assigned_sonnet(),
            usage=scm._usage_of(RESPONSE),
        )
        assert seen["loop"] is not caller


# ---------------------------------------------------------------------------
# Kept out of the prompt-cache classifier
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
PRINTS = {"toolConfigHash": "t1", "systemPromptHash": "s1"}


class _Table:
    def __init__(self, items):
        self.items = items
        self.queried = False

    def query(self, **_kwargs):
        self.queried = True
        return {"Items": self.items}


def _chat_meta(model_id: str = SONNET) -> MessageMetadata:
    return MessageMetadata(
        prefixFingerprints=dict(PRINTS),
        token_usage=TokenUsage(
            input_tokens=10, output_tokens=5, total_tokens=15,
            cache_read_input_tokens=5000, cache_write_input_tokens=100,
        ),
        model_info=ModelInfo(model_id=model_id, model_name=model_id),
    )


def _row(seconds_ago: int, **extra: Any) -> Dict[str, Any]:
    return {
        "timestamp": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "tokenUsage": {"cacheReadInputTokens": Decimal(5000), "cacheWriteInputTokens": Decimal(0)},
        **extra,
    }


def _derive(table, meta):
    return md._derive_cache_observability(
        session_id="s1", table=table, timestamp=NOW.isoformat(), message_metadata=meta
    )


class TestCacheClassifier:
    @pytest.mark.parametrize("kind", sorted(scm.SIDE_CALL_KINDS))
    def test_a_side_call_row_is_not_classified(self, kind):
        table = _Table([_row(30, prefixFingerprints=dict(PRINTS), modelInfo={"modelId": SONNET})])
        meta = MessageMetadata(
            callKind=kind,
            token_usage=TokenUsage(input_tokens=400, output_tokens=12, total_tokens=412),
            model_info=ModelInfo(model_id=MICRO, model_name=MICRO),
        )
        assert _derive(table, meta) == {}
        assert not table.queried

    @pytest.mark.parametrize(
        "side_row",
        [
            _row(5, callKind="tool_summary", modelInfo={"modelId": MICRO}),
            _row(5, callKind="session_title", modelInfo={"modelId": MICRO}),
            _row(5, callKind="compaction_summary", modelInfo={"modelId": MICRO}),
            _row(5, messageId="extract:tu-1", modelInfo={"modelId": "us.anthropic.claude-haiku-5-5"}),
        ],
    )
    def test_a_side_call_row_is_never_a_chat_calls_predecessor(self, side_row):
        """A Nova Micro summary between two chat calls must not mark the next one
        ``modelSwitched`` (which subtracts its waste as explained) or shorten its gap."""
        chat_row = _row(60, prefixFingerprints=dict(PRINTS), modelInfo={"modelId": SONNET})
        result = _derive(_Table([side_row, chat_row]), _chat_meta())
        assert "modelSwitched" not in result
        assert "agentSwitched" not in result
        assert result["cacheGapSeconds"] == 60
        assert result["cacheStatus"] == "hit"

    def test_the_window_reads_past_one_summary_per_chat_call(self):
        assert md._CACHE_PREDECESSOR_LOOKBACK >= 20


class TestLedger:
    @pytest.mark.parametrize("kind", sorted(scm.SIDE_CALL_KINDS))
    def test_counted_under_its_kind_never_as_a_turn(self, kind):
        row = {
            "callKind": kind,
            "timestamp": NOW.isoformat(),
            "appVersion": "1.40.0",
            "build": "abc",
            "cost": {"total": Decimal("0.001")},
            "tokenUsage": {"inputTokens": 400, "outputTokens": 12},
            "modelInfo": {"modelId": SONNET},
        }
        assert kind in ledger_rollups.CALL_KINDS
        assert ledger_rollups.call_kind(row) == kind
        assert not ledger_rollups.is_turn_row(row)
        version = next(u for u in ledger_rollups.ledger_updates(row) if u.pk == "LEDGER#VERSION")
        assert version.add[f"callKind.{kind}"] == 1
        assert version.add[f"callKindCost.{kind}"] == Decimal("0.001")
        assert "turns" not in version.add
        assert "chatCost" not in version.add


class TestUnpricedDefault:
    @pytest.mark.asyncio
    async def test_the_row_is_marked_and_the_ledger_leaves_it_out_of_unmetered(self, stored, unmetered):
        await scm.record_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=_uncataloged_default(),
            usage=scm._usage_of(RESPONSE),
        )
        meta = stored[0]["message_metadata"]
        assert meta.model_extra[scm.UNPRICED_DEFAULT] is True
        row = {
            **meta.model_dump(by_alias=True, exclude_none=True),
            "timestamp": NOW.isoformat(), "appVersion": "1.40.0", "build": "abc",
        }
        version = next(u for u in ledger_rollups.ledger_updates(row) if u.pk == "LEDGER#VERSION")
        assert "unmeteredCalls" not in version.add
        assert version.add["callKind.tool_summary"] == 1
        assert version.add["inputTokens"] == 400

    @pytest.mark.asyncio
    async def test_an_assigned_model_with_no_price_still_counts_as_unmetered(self, stored, unmetered, monkeypatch):
        from apis.shared.costs import pricing_config

        async def no_row(_model_id):
            return None

        monkeypatch.setattr(pricing_config, "get_model_by_model_id", no_row)
        choice = TaskModelChoice(TaskModelSlot.TOOL_SUMMARY, SONNET, None, SOURCE_ASSIGNED, None)
        await scm.record_side_call_cost(
            call_kind=scm.TOOL_SUMMARY, session_id="s1", user_id="u1", choice=choice, usage=scm._usage_of(RESPONSE),
        )
        meta = stored[0]["message_metadata"]
        assert scm.UNPRICED_DEFAULT not in meta.model_extra
        row = {**meta.model_dump(by_alias=True, exclude_none=True), "timestamp": NOW.isoformat()}
        version = next(u for u in ledger_rollups.ledger_updates(row) if u.pk == "LEDGER#VERSION")
        assert version.add["unmeteredCalls"] == 1

    def test_the_backfill_reads_the_mark(self):
        from apis.shared.observability.content_policy import LEDGER_ROW_PROJECTION

        assert scm.UNPRICED_DEFAULT in LEDGER_ROW_PROJECTION


def test_the_kind_sets_match_the_writers_constants():
    assert scm.SIDE_CALL_KINDS == {scm.SESSION_TITLE, scm.TOOL_SUMMARY, scm.COMPACTION_SUMMARY}
    assert scm.SIDE_CALL_KINDS <= set(ledger_rollups.CALL_KINDS)
    assert "document_extract" in scm.PREFIX_UNRELATED_CALL_KINDS
