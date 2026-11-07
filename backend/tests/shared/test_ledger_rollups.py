"""The release ledger's per-call rollups (docs/specs/release-ledger.md §5.2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.observability import content_policy as cp
from apis.shared.sessions import ledger_rollups as lr
from apis.shared.sessions.ledger_rollups import (
    LedgerUpdate,
    call_kind,
    ledger_updates,
    merge_updates,
    update_item_kwargs,
)


def _row(**overrides: Any) -> Dict[str, Any]:
    """A fully populated chat turn's last row, as `_store_message_metadata_cloud` stores it."""
    row: Dict[str, Any] = {
        "PK": "USER#u-1",
        "SK": "C#2026-11-06T22:10:00+00:00#abc",
        "userId": "u-1",
        "sessionId": "s-1",
        "messageId": 7,
        "timestamp": "2026-11-06T22:10:00.123456+00:00",
        "appVersion": "1.33.0",
        "build": "8f676bb2e1a4c0d2",
        "modelInfo": {
            "modelId": "us.anthropic.claude-haiku-4-5",
            "modelName": "Claude Haiku 4.5",
            "pricingSnapshot": {"inputPricePerMtok": Decimal("1.1"), "outputPricePerMtok": Decimal("5.5")},
        },
        "tokenUsage": {
            "inputTokens": 100,
            "outputTokens": 50,
            "totalTokens": 150,
            "cacheReadInputTokens": 9000,
            "cacheWriteInputTokens": 1000,
        },
        "cost": {"total": Decimal("0.0123"), "inputCost": Decimal("0.0001")},
        "cacheStatus": "hit",
        "wastedUsd": Decimal("0"),
        "turnAgentId": "agent-1",
        "projectId": "proj-1",
        "outcome": "completed",
        "turnDurationMs": 4200,
        "latency": {"timeToFirstToken": 800, "endToEndLatency": 3000},
        "toolCalls": {
            "browse_web": {"calls": 2, "errors": 1, "resultChars": 8000, "inputChars": 120,
                           "durationMs": 900, "timedCalls": 2},
            "get_my_quota": {"calls": 1, "errors": 0, "resultChars": 400, "inputChars": 2},
        },
        "compactionEvents": [{"kind": "checkpoint", "summaryTokens": 300}, {"kind": "applied"}],
        "hasDocuments": True,
        "documentTokens": 1500,
        "documentReads": {"calls": 2, "pages": 5},
        "prefixFingerprints": {"toolConfigHash": "x", "systemPromptHash": "y"},
        "contextBreakdown": {"items": [{"label": "my secret skill"}]},
    }
    row.update(overrides)
    return row


def test_a_turns_last_row_writes_every_family():
    updates = ledger_updates(_row())
    pks = [u.pk for u in updates]
    assert pks == [
        "LEDGER#VERSION",
        "LEDGER#MODEL#2026-11",
        "LEDGER#TOOL#2026-11",
        "LEDGER#TOOL#2026-11",
        "LEDGER#AGENT#2026-11",
        "LEDGER#PROJECT#2026-11",
    ]
    prefix = "2026-11-06#1.33.0#8f676bb2e1a4c0d2"
    assert [u.sk for u in updates] == [
        prefix,
        f"{prefix}#us.anthropic.claude-haiku-4-5",
        f"{prefix}#browse_web",
        f"{prefix}#get_my_quota",
        f"{prefix}#agent-1",
        f"{prefix}#proj-1",
    ]


def test_version_counters():
    version = ledger_updates(_row())[0].add
    assert version == {
        "calls": 1,
        "callKind.chat": 1,
        "turns": 1,
        "cost": Decimal("0.0123"),
        "inputTokens": 100,
        "outputTokens": 50,
        "cacheReadTokens": 9000,
        "cacheWriteTokens": 1000,
        "toolCalls": 3,
        "toolErrors": 1,
        "toolResultChars": 8400,
        "toolInputChars": 122,
        "documentReads": 2,
        "documentTokens": 1500,
        "documentTurns": 1,
        "compactionEvents": 1,
        "firstTokenMsSum": 800,
        "firstTokenCount": 1,
        "turnDurationMsSum": 4200,
        "outcomeTurns": 1,
        "completedTurns": 1,
    }


def test_a_turns_earlier_rows_are_calls_not_turns():
    """Only the last row carries `turnDurationMs`; the turn counters read it alone."""
    earlier = _row(turnDurationMs=None, emptyAnswer=None)
    earlier.pop("turnDurationMs")
    version = ledger_updates(earlier)[0].add
    assert version["calls"] == 1
    for counter in ("turns", "outcomeTurns", "completedTurns", "documentTurns", "turnDurationMsSum"):
        assert counter not in version
    model = ledger_updates(earlier)[1].add
    assert "turns" not in model


@pytest.mark.parametrize("outcome, counter", sorted(lr.OUTCOME_COUNTERS.items()))
def test_every_outcome_has_its_counter(outcome, counter):
    version = ledger_updates(_row(outcome=outcome))[0].add
    assert version[counter] == 1
    assert version["outcomeTurns"] == 1


def test_paused_is_counted_and_the_vocabulary_matches_the_coordinator():
    from agents.main_agent.streaming import stream_coordinator

    assert stream_coordinator.TURN_OUTCOMES is lr.TURN_OUTCOMES
    assert set(lr.OUTCOME_COUNTERS) == lr.TURN_OUTCOMES
    assert ledger_updates(_row(outcome="paused"))[0].add["pausedTurns"] == 1


def test_a_row_from_before_outcomes_is_a_turn_with_no_outcome():
    row = _row()
    row.pop("outcome")
    version = ledger_updates(row)[0].add
    assert version["turns"] == 1
    assert "outcomeTurns" not in version


def test_an_empty_answer_needs_the_turn_row():
    assert ledger_updates(_row(emptyAnswer=True))[0].add["emptyAnswerTurns"] == 1


@pytest.mark.parametrize(
    "overrides, kind",
    [
        pytest.param({"callKind": "handoff_brief", "messageId": None}, "handoff_brief", id="brief"),
        pytest.param({"messageId": "voice:12"}, "voice", id="voice"),
        pytest.param({"sessionId": "api-converse-key9", "messageId": 0}, "api_converse", id="api-converse"),
        pytest.param({"messageId": "extract:tooluse-1"}, "document_extract", id="extract"),
        pytest.param({"callKind": "Robert'); DROP"}, "other", id="unexpected-kind"),
    ],
)
def test_a_call_that_is_not_a_turn_counts_under_its_kind(overrides, kind):
    row = _row(**overrides)
    assert call_kind(row) == kind
    version = ledger_updates(row)[0].add
    assert version[f"callKind.{kind}"] == 1
    assert version["calls"] == 1
    # Even with a duration on the row, only a chat call is a turn.
    assert "turns" not in version
    assert "outcomeTurns" not in version


def test_cache_status_counters_split_explained_misses_like_the_anatomy():
    avoidable = ledger_updates(_row(cacheStatus="miss_avoidable", wastedUsd=Decimal("0.5"), agentSwitched=True))[0].add
    assert avoidable["avoidableMissCount"] == 1
    assert avoidable["agentSwitchMissCount"] == 1
    assert avoidable["agentSwitchUsd"] == Decimal("0.5")
    assert avoidable["wastedUsd"] == Decimal("0.5")
    assert "modelSwitchUsd" not in avoidable

    model_switch = ledger_updates(_row(cacheStatus="miss_avoidable", wastedUsd=Decimal("0.2"), modelSwitched=True))[0].add
    assert model_switch["modelSwitchUsd"] == Decimal("0.2")

    partial = ledger_updates(_row(cacheStatus="partial_miss", wastedUsd=Decimal("0.3"), agentSwitched=True))[0].add
    assert partial["partialMissCount"] == 1
    assert partial["partialMissUsd"] == Decimal("0.3")
    assert "agentSwitchUsd" not in partial

    assert ledger_updates(_row(cacheStatus="uncached"))[0].add["uncachedCalls"] == 1
    assert ledger_updates(_row(cacheStatus="estimated"))[0].add["estimatedCalls"] == 1


def test_a_call_with_usage_and_no_cost_is_unmetered():
    row = _row()
    row.pop("cost")
    version = ledger_updates(row)[0].add
    assert version["unmeteredCalls"] == 1
    assert "cost" not in version


def test_tool_rows_carry_weight_and_an_estimated_price():
    tools = {u.set["toolId"]: u.add for u in ledger_updates(_row()) if u.pk.startswith("LEDGER#TOOL#")}
    assert tools["browse_web"] == {
        "calls": 2,
        "errors": 1,
        "resultChars": 8000,
        "inputChars": 120,
        "durationMsSum": 900,
        "timedCalls": 2,
        # 8,000 chars / 4 = 2,000 tokens at $1.10/MTok.
        "estInputUsd": Decimal("0.0022000000"),
    }
    # No duration reported: neither the sum nor the count moves.
    assert "timedCalls" not in tools["get_my_quota"]


def test_a_row_from_before_the_census_weighed_results_prices_nothing():
    row = _row(toolCalls={"browse_web": {"calls": 3, "errors": 0}})
    (tool,) = [u for u in ledger_updates(row) if u.pk.startswith("LEDGER#TOOL#")]
    assert tool.add == {"calls": 3}


def test_agent_and_project_rows_only_when_the_call_ran_under_one():
    row = _row()
    row.pop("turnAgentId")
    row.pop("projectId")
    pks = [u.pk for u in ledger_updates(row)]
    assert not any(pk.startswith(("LEDGER#AGENT", "LEDGER#PROJECT")) for pk in pks)


def test_an_unstamped_row_is_keyed_unknown_and_an_undatable_row_writes_nothing():
    row = _row()
    row.pop("appVersion")
    row.pop("build")
    assert ledger_updates(row)[0].sk == "2026-11-06#unknown#unknown"
    assert ledger_updates(_row(timestamp="not a time")) == []


def test_the_date_is_utc():
    assert ledger_updates(_row(timestamp="2026-11-06T20:30:00-05:00"))[0].sk.startswith("2026-11-07#")


def test_ledger_rows_are_content_free():
    """D7: ids and numbers only — no user, session, message, title or label."""
    flat = repr([(u.pk, u.sk, u.add, u.set) for u in ledger_updates(_row())])
    for leaked in ("u-1", "s-1", "my secret skill", "USER#"):
        assert leaked not in flat


def test_the_mapping_reads_nothing_outside_the_ledger_projection():
    """The live item and a backfill row read through the projection add up the same."""
    row = _row(emptyAnswer=True, usageEstimated={"inputTokens": "projected"})

    def project(node: Any, paths) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for path in paths:
            parts = path.split(".")
            src, dst = node, out
            for i, part in enumerate(parts):
                if not isinstance(src, dict) or part not in src:
                    break
                if i == len(parts) - 1:
                    dst[part] = src[part]
                else:
                    src = src[part]
                    dst = dst.setdefault(part, {})
        return out

    projected = project(row, cp.LEDGER_ROW_PROJECTION)
    as_tuples = lambda us: [(u.pk, u.sk, u.add, u.set) for u in us]  # noqa: E731
    assert as_tuples(ledger_updates(projected)) == as_tuples(ledger_updates(row))


def test_merge_folds_same_key_updates():
    merged = merge_updates([*ledger_updates(_row()), *ledger_updates(_row(outcome="error"))])
    version = merged[0]
    assert version.add["calls"] == 2
    assert version.add["turns"] == 2
    assert version.add["completedTurns"] == 1
    assert version.add["errorTurns"] == 1
    assert version.add["cost"] == Decimal("0.0246")
    assert len(merged) == 6


def test_update_item_aliases_every_name_including_the_dotted_kind():
    kwargs = update_item_kwargs(LedgerUpdate("LEDGER#VERSION", "sk", {"calls": 1, "callKind.chat": 1}, {"date": "d"}))
    assert kwargs["Key"] == {"PK": "LEDGER#VERSION", "SK": "sk"}
    assert kwargs["UpdateExpression"] == "ADD #a0 :a0, #a1 :a1 SET #s0 = :s0"
    assert kwargs["ExpressionAttributeNames"] == {"#a0": "callKind.chat", "#a1": "calls", "#s0": "date"}
    assert kwargs["ExpressionAttributeValues"] == {":a0": 1, ":a1": 1, ":s0": "d"}


def test_the_live_writer_marks_when_counting_began_without_moving_it():
    kwargs = update_item_kwargs(LedgerUpdate("P", "S", {"calls": 1}), first_live_at="2026-11-07T00:00:00+00:00")
    assert kwargs["UpdateExpression"].endswith("SET #firstLiveAt = if_not_exists(#firstLiveAt, :firstLiveAt)")
    assert kwargs["ExpressionAttributeNames"]["#firstLiveAt"] == "firstLiveAt"


@pytest.mark.asyncio
async def test_the_writer_issues_every_update_and_swallows_failures():
    table = MagicMock()
    table.update_item.side_effect = [None, RuntimeError("throttled"), None, None, None, None]
    failed = await lr.write_ledger_updates(ledger_updates(_row()), table)
    assert failed == 1
    assert table.update_item.call_count == 6
    assert all("if_not_exists(#firstLiveAt" in c.kwargs["UpdateExpression"] for c in table.update_item.call_args_list)


@pytest.mark.asyncio
async def test_the_flag_gates_every_write(monkeypatch):
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "rollups")
    table = MagicMock()
    with patch("apis.shared.aws_clients.get_dynamodb_table", return_value=table):
        for value in ("", "false", "yes", "1"):
            monkeypatch.setenv("RELEASE_LEDGER_ENABLED", value)
            await lr.bump_ledger_for_row(_row())
        table.update_item.assert_not_called()

        monkeypatch.setenv("RELEASE_LEDGER_ENABLED", "True")
        await lr.bump_ledger_for_row(_row())
        assert table.update_item.call_count == 6


@pytest.mark.asyncio
async def test_a_failing_ledger_never_raises(monkeypatch):
    monkeypatch.setenv("RELEASE_LEDGER_ENABLED", "true")
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "rollups")
    with patch("apis.shared.aws_clients.get_dynamodb_table", side_effect=RuntimeError("no creds")):
        await lr.bump_ledger_for_row(_row())


@pytest.mark.asyncio
async def test_system_rollups_and_the_ledger_run_side_by_side(monkeypatch):
    """Neither side can skip the other: the ROLLUP# bumps failing still writes the ledger."""
    from apis.shared.sessions import metadata

    ledger = AsyncMock()
    with (
        patch.object(metadata, "_bump_rollup_rows", AsyncMock(return_value=None)) as rollups,
        patch("apis.shared.sessions.ledger_rollups.bump_ledger_for_row", ledger),
    ):
        await metadata._update_system_rollups_async(
            user_id="u-1", period="2026-11", date="2026-11-06", cost=0.1, usage_delta={},
            cache_savings=0.0, model_id="m", model_name="M", provider="bedrock", ledger_row={"x": 1},
        )
    rollups.assert_awaited_once()
    ledger.assert_awaited_once_with({"x": 1})


@pytest.mark.asyncio
async def test_the_rollup_bumps_swallow_their_own_failures(monkeypatch):
    from apis.shared.sessions import metadata

    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "rollups")
    with patch("apis.shared.storage.dynamodb_storage.DynamoDBStorage", side_effect=RuntimeError("boom")):
        await metadata._bump_rollup_rows(
            user_id="u-1", period="2026-11", date="2026-11-06", cost=0.1, usage_delta={},
            cache_savings=0.0, model_id="m", model_name="M", provider="bedrock",
        )


def test_nothing_here_runs_before_the_first_token():
    """TTFT guard (§8): the writer is reached only from the post-stream metadata
    write's fire-and-forget rollup task, never from a hook or the turn prelude."""
    from pathlib import Path

    src = Path(lr.__file__).resolve().parents[3]
    callers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if path.name != "ledger_rollups.py" and "bump_ledger_for_row" in path.read_text(encoding="utf-8")
    )
    assert callers == ["apis/shared/sessions/metadata.py"]
