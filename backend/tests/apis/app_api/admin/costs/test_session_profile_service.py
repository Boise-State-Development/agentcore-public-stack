"""`AdminCostService.get_session_profile` — the per-conversation diagnostic.

Pins the derivations an admin (or a model handed the JSON) reasons from:
the context trajectory, fingerprint churn with its agent-switch explanation,
the attachment and tool censuses, the coverage flags, and that the diagnoses
are computed on the refined per-call facts rather than the row's rollups.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _row(**overrides):
    row = {
        "sessionId": "s1",
        "userId": "u1",
        "status": "active",
        "messageCount": 8,
        "totalCost": 1.0,
        "lastContextTokens": 50_000,
        "contextWindow": 200_000,
        "totalCacheReadTokens": 1,
        "totalCacheWriteTokens": 1,
        "preferences": {"lastModel": "m1", "enabledTools": ["calculator", "analyze_spreadsheet"]},
        "compaction": {"checkpoint": 3, "truncationAnchor": 3, "totalSummarizedTurns": 1, "summaryChars": 800},
    }
    row.update(overrides)
    return row


def _call(i, *, ctx_in=100, read=0, write=0, status="hit", model="m1", switched=False, fp=None, tool_calls=None, cost=0.01):
    rec = {
        "timestamp": f"2026-09-02T00:00:{i:02d}Z",
        "messageId": i,
        "tokenUsage": {"inputTokens": ctx_in, "outputTokens": 5, "cacheReadInputTokens": read, "cacheWriteInputTokens": write},
        "modelInfo": {"modelId": model},
        "cost": {"total": cost},
        "cacheStatus": status,
        "agentSwitched": switched,
    }
    if fp is not None:
        rec["prefixFingerprints"] = fp
    if tool_calls is not None:
        rec["toolCalls"] = tool_calls
    return rec


def _service(row, records, files=None, period_cost=None):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_session_diagnostic_row = AsyncMock(return_value=row)
    service.storage.get_session_cost_records = AsyncMock(return_value=records)
    service.storage.get_user_cost_summary = AsyncMock(
        return_value={"totalCost": period_cost} if period_cost is not None else None
    )
    service._file_repository = AsyncMock()
    service._file_repository.list_session_file_stats = AsyncMock(return_value=files or [])
    return service


@pytest.mark.asyncio
async def test_none_when_the_session_has_no_row():
    assert await _service(None, []).get_session_profile("nope") is None


@pytest.mark.asyncio
async def test_trajectory_is_the_three_bucket_sum_in_call_order():
    records = [
        _call(0, ctx_in=1_000, read=0, write=4_000, status="first_write"),
        _call(1, ctx_in=200, read=4_000, write=300, status="hit"),
        _call(2, ctx_in=150, read=4_300, write=200, status="hit"),
    ]
    p = await _service(_row(), records).get_session_profile("s1")
    assert [pt.context_tokens for pt in p.context_trajectory] == [5_000, 4_500, 4_650]
    assert [pt.call_index for pt in p.context_trajectory] == [0, 1, 2]
    assert p.peak_context_tokens == 5_000
    assert p.call_count == 3
    assert p.write_read_ratio == round(4_500 / 8_300, 3)
    assert p.context_trajectory[0].cost == 0.01


@pytest.mark.asyncio
async def test_peak_falls_back_to_the_row_when_there_are_no_call_rows():
    p = await _service(_row(lastContextTokens=77_000, totalCacheReadTokens=10, totalCacheWriteTokens=5), []).get_session_profile("s1")
    assert p.peak_context_tokens == 77_000
    assert p.write_read_ratio == 0.5  # from the rollups
    assert p.context_trajectory == []


@pytest.mark.asyncio
async def test_model_mix_and_fingerprint_churn_with_agent_switch_explanation():
    fp_a = {"systemPromptHash": "sA", "toolConfigHash": "tA", "historyHash": "h", "messageCount": 1}
    fp_b = {"systemPromptHash": "sB", "toolConfigHash": "tB", "historyHash": "h", "messageCount": 2}
    fp_c = {"systemPromptHash": "sC", "toolConfigHash": "tA", "historyHash": "h", "messageCount": 3}
    records = [
        _call(0, model="m1", fp=fp_a),
        _call(1, model="m2", fp=fp_b, switched=True),   # explained: an @-mention
        _call(2, model="m1", fp=fp_c),                  # unexplained system-prompt change
    ]
    p = await _service(_row(), records).get_session_profile("s1")
    assert p.model_mix == {"m1": 2, "m2": 1}
    assert p.fingerprint_changes.system_prompt == 2
    assert p.fingerprint_changes.tool_config == 2
    assert p.fingerprint_changes.explained_by_agent_switch == 1
    assert p.data_coverage.fingerprints is True
    # Distinct hashes among NON-switched calls: sA, sC → SYSTEM_PROMPT_MUTATED fires;
    # tool hashes among non-switched calls are both tA → TOOLCONFIG_MUTATED does not.
    codes = {d.code for d in p.diagnoses}
    assert "SYSTEM_PROMPT_MUTATED" in codes
    assert "TOOLCONFIG_MUTATED" not in codes


@pytest.mark.asyncio
async def test_attachments_are_counted_by_mime_and_bytes_never_named():
    files = [
        {"uploadId": "a", "mimeType": "application/pdf", "sizeBytes": 3_000_000},
        {"uploadId": "b", "mimeType": "application/pdf", "sizeBytes": 2_500_000},
        {"uploadId": "c", "mimeType": "text/csv", "sizeBytes": 10},
    ]
    p = await _service(_row(), [], files=files).get_session_profile("s1")
    assert p.attachments.count == 3
    assert p.attachments.total_bytes == 5_500_010
    assert p.attachments.by_mime == {"application/pdf": 2, "text/csv": 1}
    assert "ATTACHMENT_HEAVY" in {d.code for d in p.diagnoses}
    assert "filename" not in p.model_dump(by_alias=True)["attachments"]


@pytest.mark.asyncio
async def test_a_failing_file_repository_degrades_to_an_empty_profile():
    service = _service(_row(), [])
    service._file_repository.list_session_file_stats = AsyncMock(side_effect=RuntimeError("no table"))
    p = await service.get_session_profile("s1")
    assert p.attachments.count == 0 and p.attachments.total_bytes == 0


@pytest.mark.asyncio
async def test_tool_census_aggregates_per_call_entries_and_sets_coverage():
    records = [
        _call(0, tool_calls={"list_assignments": {"calls": 2, "errors": 0}}),
        _call(1, tool_calls={"list_assignments": {"calls": 1, "errors": 1}, "calculator": {"calls": 1, "errors": 0}}),
        _call(2),  # a call with no tools
    ]
    p = await _service(_row(), records).get_session_profile("s1")
    assert p.tool_census["list_assignments"].calls == 3
    assert p.tool_census["list_assignments"].errors == 1
    assert p.tool_census["calculator"].calls == 1
    assert p.context_trajectory[1].tool_calls == {"list_assignments": 1, "calculator": 1}
    assert p.context_trajectory[2].tool_calls is None
    assert p.data_coverage.tool_census is True
    assert p.session.tool_call_count == 4 and p.session.tool_error_count == 1


@pytest.mark.asyncio
async def test_coverage_flags_are_honest_when_nothing_optional_was_recorded():
    p = await _service(_row(), [_call(0)]).get_session_profile("s1")
    assert p.data_coverage.tool_census is False
    assert p.data_coverage.compaction_count is False
    assert p.data_coverage.fingerprints is False
    assert p.data_coverage.cost is True
    assert p.tool_census == {}
    assert p.session.tool_call_count is None


@pytest.mark.asyncio
async def test_diagnoses_use_the_refined_peak_not_the_rows_last_context(monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD", raising=False)
    # The row's last context is under the threshold, but an earlier call peaked over it.
    records = [
        _call(0, ctx_in=1_000, read=0, write=150_000, status="first_write"),
        _call(1, ctx_in=500, read=10_000, write=140_000, status="partial_miss"),
        _call(2, ctx_in=500, read=10_000, write=140_000, status="partial_miss"),
    ]
    p = await _service(_row(lastContextTokens=20_000), records).get_session_profile("s1")
    assert p.peak_context_tokens == 151_000
    codes = [d.code for d in p.diagnoses]
    assert codes[0] == "PREFIX_SPIRAL"  # high sorts first
    assert "OVER_COMPACTION_THRESHOLD" in codes
    assert p.session.diagnosis_count == len(p.diagnoses)
    assert p.session.top_diagnosis_severity == "high"
    assert p.compaction_threshold == 100_000


@pytest.mark.asyncio
async def test_share_of_user_period_uses_the_current_month():
    p = await _service(_row(totalCost=6.0), [], period_cost=8.0).get_session_profile("s1")
    assert p.session.share_of_user_period == 75.0
    assert "DOMINANT_SESSION" in {d.code for d in p.diagnoses}


@pytest.mark.asyncio
async def test_agent_cache_bypass_names_the_offending_ids(monkeypatch):
    # Every injected family is cache-eligible today, so the rule is silent on
    # the live sets; un-promote spreadsheet analysis for this test to prove the
    # profile still surfaces the diagnosis when a family does bypass.
    from apis.app_api.admin.costs import diagnoses as dg

    monkeypatch.setattr(dg, "KEY_DESCRIBED_INJECTED_TOOL_IDS", frozenset())
    p = await _service(_row(), []).get_session_profile("s1")
    bypass = next(d for d in p.diagnoses if d.code == "AGENT_CACHE_BYPASS")
    assert bypass.evidence["bypassingToolIds"] == ["analyze_spreadsheet"]
    assert p.enabled_tool_ids == ["analyze_spreadsheet", "calculator"]


@pytest.mark.asyncio
async def test_agent_cache_bypass_is_silent_when_every_family_is_promoted():
    p = await _service(_row(), []).get_session_profile("s1")
    assert not [d for d in p.diagnoses if d.code == "AGENT_CACHE_BYPASS"]


@pytest.mark.asyncio
async def test_prefix_split_larger_than_its_own_prompt_reads_as_not_tracked():
    """`prefixTokens.tools` is a residual between two estimators and rows
    written before the write-side guard can claim a static prefix bigger than
    the whole prompt the provider billed — prod session 7f5f207f reported
    tools=223,782 against a 55,783-token prompt. Nothing is backfilled, so the
    read path drops those rather than render a number a reader would size a
    tool budget from."""
    records = [_call(0, read=10_000), _call(1, read=10_000)]
    # 60,000 tokens of "static prefix" inside a 10,100-token prompt.
    records[1]["prefixTokens"] = {"system": 12_000, "tools": 48_000}
    records[1]["windowRemovedMessages"] = 4

    p = await _service(_row(), records).get_session_profile("s1")

    assert p.prefix_tokens is None
    assert p.data_coverage.prefix_tokens is False
    # Only the split is in doubt; the rest of the ledger still decodes.
    assert p.window_removed_messages == 4


@pytest.mark.asyncio
async def test_the_whole_profile_serializes_without_content_bearing_keys():
    from apis.shared.observability.content_policy import content_bearing_paths
    files = [{"uploadId": "a", "mimeType": "application/pdf", "sizeBytes": 1}]
    p = await _service(_row(), [_call(0, tool_calls={"t": {"calls": 1, "errors": 0}})], files=files).get_session_profile("s1")
    assert content_bearing_paths(p.model_dump(by_alias=True)) == []


@pytest.mark.asyncio
async def test_context_ledger_is_decoded_diffed_and_covered():
    records = [
        _call(0, read=10_000),
        _call(1, read=10_000),
        _call(2, read=10_000),
        _call(3, read=10_000),
    ]
    # Rows carry the ledger from call 1 on: a stable prefix split, a window
    # count that rises before call 3 (a trim), and one compaction decision
    # with the summary's size at that moment.
    # Sized to fit inside the row's own billed prompt (ctx_in + read = 10,100):
    # `prefixTokens` is reconciled against it on read, so a split that could not
    # fit is dropped as not-tracked — see the test below.
    records[1]["prefixTokens"] = {"system": 1_200, "tools": 4_800}
    records[1]["windowRemovedMessages"] = 0
    records[2]["windowRemovedMessages"] = 0
    records[3]["windowRemovedMessages"] = 8
    records[3]["compactionEvents"] = [{"kind": "applied", "checkpoint": 12, "summaryTokens": 2_300}]

    p = await _service(_row(), records).get_session_profile("s1")

    assert p.prefix_tokens.system == 1_200 and p.prefix_tokens.tools == 4_800
    assert p.window_trim_calls == 1
    assert p.window_removed_messages == 8
    assert p.compaction_event_counts == {"applied": 1}
    assert p.last_summary_tokens == 2_300
    assert p.data_coverage.prefix_tokens and p.data_coverage.window_trim and p.data_coverage.compaction_events
    trimmed = [pt.window_trimmed for pt in p.context_trajectory]
    assert trimmed == [None, 0, 0, 8]
    assert p.context_trajectory[3].compaction == ["applied"]


@pytest.mark.asyncio
async def test_rows_without_a_ledger_read_not_tracked():
    p = await _service(_row(), [_call(0), _call(1)]).get_session_profile("s1")
    assert p.prefix_tokens is None
    assert p.window_removed_messages is None and p.window_trim_calls == 0
    assert p.compaction_event_counts == {} and p.last_summary_tokens is None
    assert not p.data_coverage.prefix_tokens and not p.data_coverage.window_trim
    assert not p.data_coverage.compaction_events
    assert all(pt.window_trimmed is None and pt.compaction is None for pt in p.context_trajectory)


@pytest.mark.asyncio
async def test_session_row_counters_alone_mark_compaction_events_as_tracked():
    p = await _service(_row(compactionAppliedCount=0), [_call(0)]).get_session_profile("s1")
    assert p.data_coverage.compaction_events
    assert p.session.compaction_applied_count == 0


# ── feedback join (document-context offload PR-7) ───────────────────────────


def _feedback(message_id, value, reason=None):
    row = {"sessionId": "s1", "messageId": message_id, "value": value, "updatedAt": "2026-09-16T00:00:00Z"}
    if reason:
        row["reason"] = reason
    return row


def _service_with_feedback(row, records, feedback):
    service = _service(row, records)
    service.storage.get_session_feedback_rows = AsyncMock(return_value=feedback)
    return service


@pytest.mark.asyncio
async def test_feedback_joins_the_turn_class_when_the_rows_carry_it():
    records = [
        _call(0),  # attach turn: full document inline
        _call(1),  # follow-up: digest only
        _call(2),  # follow-up that read pages back
        _call(3),  # no documents at all
    ]
    records[0]["hasDocuments"] = True
    records[1]["hasDocuments"] = False
    records[1]["documentDigests"] = 1
    records[2]["hasDocuments"] = False
    records[2]["documentDigests"] = 1
    records[2]["documentReads"] = {"calls": 1, "pages": 4, "bytes": 1000}
    records[3]["hasDocuments"] = False
    records[3]["documentDigests"] = 0
    feedback = [
        _feedback(0, 1),
        _feedback(1, -1, "wrong"),
        _feedback(2, 1),
        _feedback(3, -1, "tool_failed"),
        _feedback(9, -1),  # no cost row for this message
    ]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")

    assert (p.feedback.up, p.feedback.down, p.feedback.unjoined) == (2, 3, 1)
    by = p.feedback.by_turn_class
    assert by is not None
    assert (by.full.up, by.full.down) == (1, 0)
    assert (by.digest_only.up, by.digest_only.down) == (0, 1)
    assert (by.retrieved.up, by.retrieved.down) == (1, 0)
    assert (by.none.up, by.none.down) == (0, 1)
    assert p.data_coverage.feedback is True
    # Wire shape the SPA reads.
    wire = p.model_dump(by_alias=True)["feedback"]
    assert wire["byTurnClass"]["digestOnly"] == {"up": 0, "down": 1}


@pytest.mark.asyncio
async def test_feedback_counts_without_turn_class_when_rows_predate_1137():
    records = [_call(0), _call(1)]  # no hasDocuments / documentDigests / documentReads
    feedback = [_feedback(0, 1), _feedback(1, -1, "instructions")]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")
    assert (p.feedback.up, p.feedback.down) == (1, 1)
    assert p.feedback.by_turn_class is None, "turn class is 'not tracked', not 'none'"
    assert p.feedback.unjoined == 0
    assert p.data_coverage.feedback is True


@pytest.mark.asyncio
async def test_no_feedback_rows_falls_back_to_rollups_and_coverage_is_honest():
    p = await _service_with_feedback(_row(), [_call(0)], []).get_session_profile("s1")
    assert (p.feedback.up, p.feedback.down) == (0, 0)
    assert p.data_coverage.feedback is False

    p = await _service_with_feedback(_row(thumbsUp=2, thumbsDown=1), [_call(0)], []).get_session_profile("s1")
    assert (p.feedback.up, p.feedback.down) == (2, 1)
    assert p.data_coverage.feedback is True
    assert p.feedback.by_turn_class is None


@pytest.mark.asyncio
async def test_retries_are_counted_and_rework_is_priced_from_the_cost_rows():
    # Turn A: assistant 1 (thumbed down, $0.10). Retry sent as user message 2;
    # its turn is assistant 3 + 4 ($0.20 + $0.05, a tool round trip). User 5,
    # assistant 6 ($0.99) is the NEXT turn and must not be counted.
    records = [_call(1, cost=0.10), _call(3, cost=0.20), _call(4, cost=0.05), _call(6, cost=0.99)]
    feedback = [{**_feedback(1, -1, "wrong"), "retryMessageId": 2}, _feedback(6, 1)]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")
    assert p.feedback.retried == 1
    assert p.feedback.rework_usd == 0.35
    assert (p.feedback.up, p.feedback.down) == (1, 1)

    # A retry whose turn has no cost rows yet (still streaming) counts, but prices only the thumbed side.
    feedback = [{**_feedback(1, -1, "wrong"), "retryMessageId": 7}]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")
    assert p.feedback.retried == 1 and p.feedback.rework_usd == 0.10

    # Nothing priced at all → None, not 0.
    feedback = [{**_feedback(9, -1), "retryMessageId": 10}]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")
    assert p.feedback.retried == 1 and p.feedback.rework_usd is None


@pytest.mark.asyncio
async def test_implicit_signal_rows_are_never_summed_into_the_thumb_counts():
    records = [_call(0)]
    records[0]["hasDocuments"] = True
    feedback = [_feedback(0, 1), {**_feedback(0, -1), "signal": "implicit"}, {**_feedback(0, -1), "signal": "explicit"}]
    p = await _service_with_feedback(_row(), records, feedback).get_session_profile("s1")
    assert (p.feedback.up, p.feedback.down) == (1, 1)
    assert (p.feedback.by_turn_class.full.up, p.feedback.by_turn_class.full.down) == (1, 1)


@pytest.mark.asyncio
async def test_implicit_signals_are_counted_as_messages_touched_per_kind():
    def implicit(message_id, kind, count):
        return {"sessionId": "s1", "messageId": message_id, "signal": "implicit", "kind": kind, "count": count, "updatedAt": "t"}

    feedback = [implicit(0, "copy", 3), implicit(2, "copy", 1), implicit(2, "continue", 1), implicit(4, "weird", 1)]
    p = await _service_with_feedback(_row(), [_call(0), _call(2)], feedback).get_session_profile("s1")
    assert p.feedback.implicit is not None
    assert (p.feedback.implicit.copied, p.feedback.implicit.continued) == (2, 1)
    assert (p.feedback.up, p.feedback.down) == (0, 0)
    assert p.data_coverage.feedback is True
    assert p.model_dump(by_alias=True)["feedback"]["implicit"] == {"copied": 2, "continued": 1}

    p = await _service_with_feedback(_row(), [_call(0)], [_feedback(0, 1)]).get_session_profile("s1")
    assert p.feedback.implicit is None


@pytest.mark.asyncio
async def test_feedback_reader_failure_never_breaks_the_profile():
    service = _service(_row(), [_call(0)])
    service.storage.get_session_feedback_rows = AsyncMock(side_effect=RuntimeError("boom"))
    p = await service.get_session_profile("s1")
    assert p is not None and p.feedback.up == 0 and p.data_coverage.feedback is False


@pytest.mark.asyncio
async def test_judged_thumbs_aggregate_per_evaluator_and_corroboration():
    def judged(message_id, reason, scores=None, corroborated=None):
        row = _feedback(message_id, -1, reason)
        row["evaluatedAt"] = "t"
        row["evaluation"] = {"reason": reason, "evaluators": list((scores or {}).keys())}
        if scores:
            row["evaluation"]["scores"] = {k: {"value": v, "n": 1} for k, v in scores.items()}
        if corroborated is not None:
            row["evaluation"]["toolFailureCorroborated"] = corroborated
        return row

    feedback = [
        judged(0, "wrong", {"Builtin.Correctness": 1.0, "Builtin.Faithfulness": 0.5}),
        judged(1, "wrong", {"Builtin.Correctness": 0.0}),
        judged(2, "tool_failed", corroborated=True),
        judged(3, "tool_failed", corroborated=False),
        _feedback(4, -1, "other"),  # not judged yet
    ]
    p = await _service_with_feedback(_row(), [_call(i) for i in range(5)], feedback).get_session_profile("s1")
    ev = p.feedback.evaluations
    assert ev is not None and ev.judged == 4
    assert ev.by_evaluator["Builtin.Correctness"].n == 2 and ev.by_evaluator["Builtin.Correctness"].mean == 0.5
    assert ev.by_evaluator["Builtin.Faithfulness"].mean == 0.5
    assert (ev.tool_failures_reported, ev.tool_failures_corroborated) == (2, 1)
    assert p.feedback.down == 5
    wire = p.model_dump(by_alias=True)["feedback"]["evaluations"]
    assert wire["byEvaluator"]["Builtin.Correctness"] == {"n": 2, "mean": 0.5}

    p = await _service_with_feedback(_row(), [_call(0)], [_feedback(0, -1)]).get_session_profile("s1")
    assert p.feedback.evaluations is None


class TestDownThumbReasons:
    """The per-session half of the reason split the fleet view reports (#1152):
    the fleet says *how much*, this says *why this conversation*.

    A closed set of codes, never free text — the write path types ``reason`` as
    a ``Literal``, so anything else is a row from a future schema and is dropped
    rather than surfaced.
    """

    def _rows(self, *reasons):
        return [
            {"messageId": i, "value": -1, **({"reason": r} if r else {})}
            for i, r in enumerate(reasons, start=1)
        ]

    def test_reasons_are_counted(self):
        from apis.app_api.admin.costs.service import _join_feedback

        profile = _join_feedback([], self._rows("tool_failed", "tool_failed", "length"))
        assert profile.reasons == {"tool_failed": 2, "length": 1}
        assert profile.down == 3

    def test_a_reasonless_down_thumb_still_counts_as_one(self):
        from apis.app_api.admin.costs.service import _join_feedback

        profile = _join_feedback([], self._rows("wrong", None))
        assert profile.reasons == {"wrong": 1}
        assert profile.down == 2

    def test_unknown_codes_are_dropped(self):
        from apis.app_api.admin.costs.service import _join_feedback

        profile = _join_feedback([], self._rows("from-a-future-schema"))
        assert profile.reasons == {} and profile.down == 1

    def test_up_thumbs_carry_no_reason(self):
        from apis.app_api.admin.costs.service import _join_feedback

        profile = _join_feedback([], [{"messageId": 1, "value": 1, "reason": "wrong"}])
        assert profile.reasons == {}, "a reason on an up-thumb is meaningless"
        assert profile.up == 1
