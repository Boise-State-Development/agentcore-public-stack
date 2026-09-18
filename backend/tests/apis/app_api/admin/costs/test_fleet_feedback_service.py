"""`AdminCostService.get_fleet_feedback` — the outcome signal for a period.

The gap this closes: the feedback join existed only on the per-session profile,
so an admin could diagnose *a* conversation but never ask "is a digest-only turn
thumbed down more often than one holding the full document?" — which is the
question the document-offload spec's quality gate is written against, and every
tuning decision the thumbs are meant to inform.

Pinned here: the fan-out reuses the session rollups as a filter (no `F#` read
for a session with no thumbs), the same `_join_feedback` the drill-down uses, and
the honesty rules from response-feedback spec §9 — every rate carries its `n`,
rates are withheld below a floor, `coverage` reports the base, a truncated sweep
says so, and no-rollups reads "not tracked" rather than "nobody complained".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _session(session_id, *, up=None, down=None, calls=10):
    row = {"sessionId": session_id, "callCount": calls}
    if up is not None:
        row["thumbsUp"] = up
    if down is not None:
        row["thumbsDown"] = down
    return row


def _call(message_id, *, has_documents=False, digests=0, pages=0, cost=0.01):
    rec = {
        "timestamp": f"2026-09-02T00:00:{message_id:02d}Z",
        "messageId": message_id,
        "tokenUsage": {"inputTokens": 100, "outputTokens": 5},
        "modelInfo": {"modelId": "m1"},
        "cost": {"total": cost},
        "cacheStatus": "hit",
        "hasDocuments": has_documents,
        "documentDigests": digests,
    }
    if pages:
        rec["documentReads"] = {"calls": 1, "pages": pages, "bytes": 900}
    return rec


def _thumb(message_id, value, *, reason=None, retry=None, signal=None):
    row = {"messageId": message_id, "value": value}
    if reason:
        row["reason"] = reason
    if retry is not None:
        row["retryMessageId"] = retry
    if signal is not None:
        row["signal"] = signal
    return row


def _service(*, users, sessions_by_user, records_by_session, feedback_by_session):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_top_users_by_cost = AsyncMock(return_value=users)
    service.storage.get_user_session_costs = AsyncMock(
        side_effect=lambda user_id, active_since=None: sessions_by_user.get(user_id, [])
    )
    service.storage.get_session_cost_records = AsyncMock(
        side_effect=lambda session_id: records_by_session.get(session_id, [])
    )
    service.storage.get_session_feedback_rows = AsyncMock(
        side_effect=lambda session_id: feedback_by_session.get(session_id, [])
    )
    return service


@pytest.mark.asyncio
async def test_empty_period_is_not_tracked_rather_than_zero():
    service = _service(users=[], sessions_by_user={}, records_by_session={}, feedback_by_session={})
    out = await service.get_fleet_feedback(period="2026-09")
    assert out.tracked is False, "no rollups anywhere means 'not tracked'"
    assert out.n == 0 and out.down_rate is None and out.coverage is None


@pytest.mark.asyncio
async def test_sessions_without_thumbs_are_never_fanned_out_over():
    """The filter that makes this affordable: a session whose rollups show no
    thumbs must not cost an F# read."""
    service = _service(
        users=[{"userId": "u1"}],
        sessions_by_user={"u1": [_session("s1", up=0, down=0), _session("s2", up=1, down=0)]},
        records_by_session={"s2": [_call(1)]},
        feedback_by_session={"s2": [_thumb(1, 1)]},
    )
    out = await service.get_fleet_feedback(period="2026-09")
    assert out.tracked is True
    assert out.sessions_with_feedback == 1 and out.sessions_scanned == 1
    scanned = {c.args[0] if c.args else c.kwargs.get("session_id")
               for c in service.storage.get_session_feedback_rows.await_args_list}
    assert scanned == {"s2"}, "s1 had no thumbs and must not have been read"


@pytest.mark.asyncio
async def test_rollups_absent_entirely_means_untracked_but_still_no_crash():
    service = _service(
        users=[{"userId": "u1"}],
        sessions_by_user={"u1": [_session("s1")]},   # no thumbsUp/thumbsDown keys at all
        records_by_session={},
        feedback_by_session={},
    )
    out = await service.get_fleet_feedback(period="2026-09")
    assert out.tracked is False and out.sessions_with_feedback == 0


class TestTheComparisonThatMatters:
    @pytest.mark.asyncio
    async def test_down_rate_splits_by_turn_class(self):
        """The document-offload question, answered from stored rows: a
        digest-only turn thumbed down more often than a full-document one."""
        records = [_call(i, has_documents=True) for i in range(1, 13)]
        records += [_call(i, digests=1) for i in range(13, 25)]
        thumbs = [_thumb(i, 1) for i in range(1, 12)] + [_thumb(12, -1)]          # full:  1/12 down
        thumbs += [_thumb(i, -1) for i in range(13, 19)] + [_thumb(i, 1) for i in range(19, 25)]  # digest: 6/12
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=17, down=7, calls=24)]},
            records_by_session={"s1": records},
            feedback_by_session={"s1": thumbs},
        )
        out = await service.get_fleet_feedback(period="2026-09")

        full, digest = out.by_turn_class["full"], out.by_turn_class["digestOnly"]
        assert (full.n, full.down) == (12, 1)
        assert (digest.n, digest.down) == (12, 6)
        assert full.down_rate == pytest.approx(1 / 12, abs=1e-4)
        assert digest.down_rate == pytest.approx(0.5, abs=1e-4)
        assert digest.down_rate > full.down_rate, "this is the signal that says widen pinning"
        # ...and each class reports the exposure the rate is drawn from.
        assert full.calls == 12 and digest.calls == 12

    @pytest.mark.asyncio
    async def test_reason_split_is_aggregated(self):
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=0, down=4)]},
            records_by_session={"s1": [_call(i) for i in range(1, 5)]},
            feedback_by_session={"s1": [
                _thumb(1, -1, reason="tool_failed"),
                _thumb(2, -1, reason="tool_failed"),
                _thumb(3, -1, reason="length"),
                _thumb(4, -1),                      # no reason given
            ]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.reasons == {"tool_failed": 2, "length": 1}
        assert out.down == 4, "a reasonless down-thumb still counts as a down-thumb"

    @pytest.mark.asyncio
    async def test_unknown_reason_codes_are_dropped(self):
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=0, down=1)]},
            records_by_session={"s1": [_call(1)]},
            feedback_by_session={"s1": [_thumb(1, -1, reason="from-a-future-schema")]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.reasons == {}

    @pytest.mark.asyncio
    async def test_rework_is_priced(self):
        """What a bad answer cost, in the same units as the rest of the cost
        surface: the thumbed answer plus the retry turn's answer.

        ``retryMessageId`` is the *user* correction (no cost row of its own);
        the retry's assistant rows are the consecutive indexes after it. So
        here message 1 is the thumbed answer, 2 is the correction the user
        typed, and 3 is the answer it produced.
        """
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=0, down=1)]},
            records_by_session={"s1": [_call(1, cost=0.02), _call(3, cost=0.03)]},
            feedback_by_session={"s1": [_thumb(1, -1, reason="wrong", retry=2)]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.retried == 1
        assert out.rework_usd == pytest.approx(0.05, abs=1e-6)


class TestHonestyRules:
    """Response-feedback spec §9 — the rules that stop this becoming a KPI."""

    @pytest.mark.asyncio
    async def test_a_rate_below_the_floor_is_withheld(self):
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=1, down=2, calls=100)]},
            records_by_session={"s1": [_call(i) for i in range(1, 4)]},
            feedback_by_session={"s1": [_thumb(1, 1), _thumb(2, -1), _thumb(3, -1)]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.n == 3
        assert out.down_rate is None, "a rate over three thumbs is not a number to act on"
        assert out.up == 1 and out.down == 2, "the counts are still reported"

    @pytest.mark.asyncio
    async def test_coverage_reports_the_base_the_rate_is_drawn_from(self):
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=10, down=2, calls=400)]},
            records_by_session={"s1": [_call(i) for i in range(1, 13)]},
            feedback_by_session={"s1": [_thumb(i, 1) for i in range(1, 11)]
                                 + [_thumb(11, -1), _thumb(12, -1)]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.assistant_calls == 400
        assert out.coverage == pytest.approx(12 / 400, abs=1e-5), "1-5% is the expected band"
        assert out.down_rate == pytest.approx(2 / 12, abs=1e-4)

    @pytest.mark.asyncio
    async def test_a_truncated_sweep_says_so(self):
        sessions = [_session(f"s{i}", up=1, down=0) for i in range(5)]
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": sessions},
            records_by_session={f"s{i}": [_call(1)] for i in range(5)},
            feedback_by_session={f"s{i}": [_thumb(1, 1)] for i in range(5)},
        )
        out = await service.get_fleet_feedback(period="2026-09", sessions_to_scan=2)
        assert out.sessions_with_feedback == 5
        assert out.sessions_scanned == 2
        assert out.truncated is True

    @pytest.mark.asyncio
    async def test_implicit_signals_are_never_summed_in(self):
        """Spec §10 shares the row family but answers a different question."""
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("s1", up=1, down=1)]},
            records_by_session={"s1": [_call(1), _call(2)]},
            feedback_by_session={"s1": [
                _thumb(1, 1),
                _thumb(2, -1, signal="implicit"),
            ]},
        )
        out = await service.get_fleet_feedback(period="2026-09")
        assert (out.up, out.down) == (1, 0)


class TestResilience:
    @pytest.mark.asyncio
    async def test_one_unreadable_session_does_not_empty_the_sweep(self):
        service = _service(
            users=[{"userId": "u1"}],
            sessions_by_user={"u1": [_session("bad", up=1, down=0), _session("ok", up=1, down=0)]},
            records_by_session={"ok": [_call(1)]},
            feedback_by_session={"ok": [_thumb(1, 1)]},
        )

        async def records(session_id):
            if session_id == "bad":
                raise RuntimeError("dynamo is having a day")
            return [_call(1)]

        service.storage.get_session_cost_records = AsyncMock(side_effect=records)
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.up == 1, "the readable session still counted"

    @pytest.mark.asyncio
    async def test_a_failed_user_fanout_returns_an_empty_summary_not_a_500(self):
        service = _service(users=[], sessions_by_user={}, records_by_session={}, feedback_by_session={})
        service.storage.get_top_users_by_cost = AsyncMock(side_effect=RuntimeError("index unavailable"))
        out = await service.get_fleet_feedback(period="2026-09")
        assert out.period == "2026-09" and out.tracked is False
