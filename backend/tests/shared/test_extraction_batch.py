"""N documents per call: the shape of the concurrency, and its failure modes.

``docs/specs/structured-extraction.md`` §17. This module owns only the shape —
bounded concurrency, warm-then-fan-out, and what a per-document failure means —
so it is testable without a model, which is the point of keeping it separate
from the extraction call.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from apis.shared.extraction import batch as b
from apis.shared.extraction.field_sets import parse_field_set

FIELD_SET = parse_field_set(
    "| name | find |\n|---|---|\n| deadline | the deadline |\n", name="s"
)


def outcome(uid: str, *, error: str = None, fatal: bool = False) -> b.DocumentOutcome:
    return b.DocumentOutcome(
        upload_id=uid,
        filename=f"{uid}.pdf",
        result=None if error else object(),  # type: ignore[arg-type]
        error=error,
        fatal_for_batch=fatal,
    )


class TestTheConcurrencyLimit:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", b.DEFAULT_CONCURRENCY),
            ("1", 1),
            ("8", 8),
            ("16", 16),
            ("0", 1),              # clamped up
            ("-5", 1),
            ("99", b.MAX_CONCURRENCY),
            ("abc", b.DEFAULT_CONCURRENCY),
            ("4.5", b.DEFAULT_CONCURRENCY),
            ("  6  ", 6),
        ],
    )
    def test_it_is_read_and_clamped(self, monkeypatch, value, expected) -> None:
        """A malformed value must narrow the blast radius, not widen it."""
        monkeypatch.setenv("DOCUMENT_EXTRACT_CONCURRENCY", value)
        assert b.concurrency_limit() == expected

    def test_unset_falls_back_to_the_conservative_default(self, monkeypatch) -> None:
        monkeypatch.delenv("DOCUMENT_EXTRACT_CONCURRENCY", raising=False)
        assert b.concurrency_limit() == 4

    def test_the_default_is_defensibly_small(self) -> None:
        """Pinned because the reason for the number is easy to lose.

        Each call is a constrained Converse request carrying a whole document,
        and this deployment has a Bedrock throttle alarm an over-eager default
        would trip on the first real batch — the worst moment to learn the
        number. Four also captures most of the win: fourteen documents
        four-wide is four waves, not fourteen.
        """
        assert b.DEFAULT_CONCURRENCY == 4
        assert b.MAX_CONCURRENCY == 16


class TestRunBounded:
    @pytest.mark.asyncio
    async def test_results_come_back_in_input_order(self) -> None:
        """Not completion order: the caller zips these against its documents."""

        async def work(i: int) -> int:
            await asyncio.sleep((10 - i) * 0.005)  # later items finish first
            return i

        results = await b.run_bounded([lambda i=i: work(i) for i in range(10)], limit=4)
        assert results == list(range(10))

    @pytest.mark.asyncio
    async def test_never_more_than_the_limit_run_at_once(self) -> None:
        live = 0
        peak = 0

        async def work() -> None:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

        await b.run_bounded([work for _ in range(20)], limit=3)
        assert peak == 3

    @pytest.mark.asyncio
    async def test_a_slow_task_does_not_idle_the_other_slots(self) -> None:
        """Why a semaphore and not chunked waves: in a chunked loop one
        300-page document stalls three idle slots until it finishes."""
        order: List[int] = []

        async def work(i: int) -> int:
            await asyncio.sleep(0.2 if i == 0 else 0.01)
            order.append(i)
            return i

        await b.run_bounded([lambda i=i: work(i) for i in range(8)], limit=2)
        # The slow first task is still running while the rest drain through the
        # other slot, so it finishes last despite starting first.
        assert order[-1] == 0
        assert len(order) == 8

    @pytest.mark.asyncio
    async def test_one_failure_is_returned_not_raised(self) -> None:
        async def ok() -> str:
            return "fine"

        async def boom() -> str:
            raise RuntimeError("nope")

        results = await b.run_bounded([ok, boom, ok], limit=2)
        assert results[0] == "fine"
        assert isinstance(results[1], RuntimeError)
        assert results[2] == "fine"

    @pytest.mark.asyncio
    async def test_cancellation_is_re_raised_not_swallowed(self) -> None:
        """A cancelled turn is not a per-document failure."""

        async def cancelled() -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await b.run_bounded([cancelled], limit=2)

    @pytest.mark.asyncio
    async def test_an_empty_list_is_a_no_op(self) -> None:
        assert await b.run_bounded([]) == []


class TestWarmThenFanOut:
    @pytest.mark.asyncio
    async def test_the_first_document_runs_alone(self) -> None:
        """§17.5. Not for latency — measurement showed parallel cold calls cost
        one compilation in wall clock — but so a capacity failure costs one
        call instead of N."""
        started: List[str] = []
        concurrent_with_first = []

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            started.append(uid)
            if uid == "d0":
                await asyncio.sleep(0.05)
                concurrent_with_first.append(list(started))
            return outcome(uid)

        await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(5)], extract_one
        )
        # While the first was in flight nothing else had started.
        assert concurrent_with_first == [["d0"]]
        assert len(started) == 5

    @pytest.mark.asyncio
    async def test_outcomes_are_in_document_order(self) -> None:
        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            await asyncio.sleep(0.01 if uid != "d1" else 0.0)
            return outcome(uid)

        outcomes = await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(4)], extract_one
        )
        assert [o.upload_id for o in outcomes] == ["d0", "d1", "d2", "d3"]

    @pytest.mark.asyncio
    async def test_a_field_set_that_cannot_compile_stops_the_batch(self) -> None:
        """Firing the rest would produce N copies of an error about the
        author's field set that no user can act on differently."""
        calls: List[str] = []

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            calls.append(uid)
            return outcome(uid, error="too complex; split the set", fatal=True)

        outcomes = await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(6)], extract_one
        )
        assert calls == ["d0"]  # the other five were never attempted
        assert len(outcomes) == 6
        assert all(o.error == "too complex; split the set" for o in outcomes)
        assert all(o.fatal_for_batch for o in outcomes)

    @pytest.mark.asyncio
    async def test_one_bad_document_does_not_stop_the_batch(self) -> None:
        """The difference that matters: an unreadable document is one
        document's problem, a wide field set is the batch's."""

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            if uid == "d0":
                return outcome(uid, error="no text layer")
            return outcome(uid)

        outcomes = await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(4)], extract_one
        )
        assert outcomes[0].error == "no text layer"
        assert [o.ok for o in outcomes] == [False, True, True, True]

    @pytest.mark.asyncio
    async def test_an_exception_from_one_document_becomes_its_reason(self) -> None:
        """Every document comes back as a result or a reason — never missing.
        A set where three failed silently is a judgement on eleven documents
        presented as a judgement on fourteen."""

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            if uid == "d2":
                raise RuntimeError("s3 exploded")
            return outcome(uid)

        outcomes = await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(4)], extract_one
        )
        assert len(outcomes) == 4
        assert outcomes[2].ok is False
        assert "s3 exploded" in outcomes[2].error

    @pytest.mark.asyncio
    async def test_a_single_document_needs_no_fan_out(self) -> None:
        calls: List[str] = []

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            calls.append(uid)
            return outcome(uid)

        outcomes = await b.extract_over_documents(FIELD_SET, [("only", "only")], extract_one)
        assert calls == ["only"]
        assert len(outcomes) == 1

    @pytest.mark.asyncio
    async def test_no_documents_is_a_no_op(self) -> None:
        async def never(uid: str, label: str) -> b.DocumentOutcome:
            raise AssertionError("should not be called")

        assert await b.extract_over_documents(FIELD_SET, [], never) == []

    @pytest.mark.asyncio
    async def test_the_limit_is_respected_across_the_fan_out(self) -> None:
        live = 0
        peak = 0

        async def extract_one(uid: str, label: str) -> b.DocumentOutcome:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return outcome(uid)

        await b.extract_over_documents(
            FIELD_SET, [(f"d{i}", f"d{i}") for i in range(12)], extract_one, limit=3
        )
        assert peak == 3


class TestTheMeasuredThresholds:
    def test_the_document_cap_allows_the_measured_range(self) -> None:
        """§17.7 measured at 10/25/50, so 50 has to be reachable."""
        assert b.MAX_DOCUMENTS >= 50

    def test_the_reliable_count_is_the_measured_one(self) -> None:
        """Pinned as a value because it came from an experiment, re-run at ten
        trials per condition (§17.8): with a planted cross-document collision
        the reduce found it 10/10 at ten documents and 8/10 at both twenty-five
        and fifty, while token cost stayed flat at ~325 per document.

        So this is where the number stops being 10/10, not a cliff — the
        earlier "roughly a coin flip above ten" was three-trial noise, and the
        correction is recorded because the first reading was the pessimistic
        one and someone could have scoped a feature around it.
        """
        assert b.RELIABLE_DOCUMENT_COUNT == 10

    def test_the_reliable_count_is_not_a_cap(self) -> None:
        """A user may legitimately want fifty documents extracted as a data
        pull even where the judgement across them is weaker."""
        assert b.RELIABLE_DOCUMENT_COUNT < b.MAX_DOCUMENTS


class TestNoTurnScopedState:
    def test_the_module_keys_nothing_on_the_current_turn(self) -> None:
        """§15: a queue-driven worker must be able to call the same functions
        with the same arguments."""
        import inspect

        source = inspect.getsource(b)
        for banned in ("_current", "_in_flight", "_last_", "session_id", "user_id"):
            assert banned not in source, f"{banned!r} ties this to a turn"
