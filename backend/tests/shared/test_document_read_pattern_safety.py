"""ReDoS safety for ``document_read`` pattern mode.

The defect: ``_compile`` passed a model-supplied regex straight to
``re.compile``. A nested-quantifier pattern then runs in exponential time —
measured on the real engine, ``(a+)+$`` against ``"a"*n + "!"`` takes 0.87 s at
n=24, 3.5 s at 26, **14 s at 28**. Extracted PDF lines run 60–100 characters, so
such a pattern never returns; the search happens in ``asyncio.to_thread``, which
CPython gives no way to cancel, so the turn hangs to the 600 s SSE timeout.

Two defences, tested here:

* ``catastrophic_pattern`` refuses the nested-quantifier family up front and the
  pattern is searched literally instead — the same graceful degradation an
  invalid regex already got — with a note carried to the model so a different
  result is never presented as the requested one.
* ``_Budget`` bounds the walk across lines and pages, so a *merely slow* pattern
  the detector does not flag degrades to a partial answer instead of hanging.
"""

from __future__ import annotations

import time

import pytest

from apis.shared.files import document_read as dr

from .test_document_read import build_pdf

# Shapes that make the backtracking engine go exponential.
CATASTROPHIC = [
    "(a+)+$",
    r"(\w+\s*)+",
    r"(\s*\w+)*",
    "(a*)*",
    "(a+|b){2,}",
    r"([a-z]+)+@",
    "(x+)+y",
]

# Patterns a model would plausibly write against a real document. None may be
# refused: a false positive silently turns a regex search into a literal one.
LEGITIMATE = [
    r"\d+",
    r"\w+\s+total",
    "(invoice|receipt)",
    "(foo)+",
    r"section \d+",
    r"(?:\d{4})-(?:\d{2})",
    "retention",
    r"[A-Z]{2,}\s",
    r"\$[\d,]+\.\d{2}",
    "(a|b)+",
    r"a{2,5}",
    r"\(\w+\)+",       # escaped parens are literals, not a group
    r"[(]a+[)]+",      # ...and so are parens inside a character class
]


class TestCatastrophicPattern:
    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_flags_nested_quantifiers(self, pattern):
        assert dr.catastrophic_pattern(pattern) is True

    @pytest.mark.parametrize("pattern", LEGITIMATE)
    def test_never_flags_a_legitimate_pattern(self, pattern):
        assert dr.catastrophic_pattern(pattern) is False

    @pytest.mark.parametrize("pattern", ["", "(", ")", "(((", "a{", "[a+)+", "\\"])
    def test_malformed_input_never_raises(self, pattern):
        assert dr.catastrophic_pattern(pattern) in (True, False)


class TestCompileFallback:
    def test_catastrophic_pattern_is_searched_literally_with_a_note(self):
        regex, note = dr._compile("(a+)+$")
        assert note is not None and "exponential" in note
        # Literal: it matches its own text, and not an exponential input.
        assert regex.search("see (a+)+$ here") is not None
        assert regex.search("aaaaaaaa!") is None

    def test_the_measured_attack_is_now_instant(self):
        """n=28 took 14 s before this change; n=80 must be immediate now."""
        regex, _ = dr._compile("(a+)+$")
        t0 = time.monotonic()
        regex.search("a" * 80 + "!")
        assert time.monotonic() - t0 < 0.05

    def test_invalid_regex_still_degrades_to_literal(self):
        regex, note = dr._compile("([")
        assert note is not None and "not a valid regular expression" in note
        assert regex.search("a ([ bracket") is not None

    def test_a_good_pattern_compiles_as_a_regex_with_no_note(self):
        regex, note = dr._compile(r"section \d+")
        assert note is None
        assert regex.search("SECTION 44 here") is not None   # IGNORECASE preserved


class TestBudget:
    def test_disabled_budget_never_expires(self):
        clock = dr._Budget(0)
        assert clock.out_of_time() is False

    def test_expires_and_latches(self):
        clock = dr._Budget(0.01)
        time.sleep(0.02)
        assert clock.out_of_time() is True
        assert clock.expired is True

    def test_grep_stops_on_an_exhausted_budget(self):
        clock = dr._Budget(0.01)
        time.sleep(0.02)
        import re
        out = dr._grep_lines(["match"] * 100, re.compile("match"), 40, clock)
        assert out == []

    def test_grep_without_a_budget_is_unchanged(self):
        import re
        out = dr._grep_lines(["alpha", "beta", "alpha"], re.compile("alpha"), 40)
        assert [m["line"] for m in out] == [1, 3]


class TestBudgetCoversTheScanOnly:
    """The budget bounds the *scan*, not the setup that precedes it.

    ``_pdf_pattern`` used to start the clock before ``_open_pdf``, so lazily
    importing the pypdfium2 native library and parsing the document were charged
    to the scan. A cold container paying enough of the budget on the open
    reported ``timed_out`` with ``pages_searched: 0`` — a timeout on a search
    that never started, and an empty answer to an ordinary pattern.
    """

    def test_the_clock_starts_after_the_pdf_is_opened(self, monkeypatch):
        """Structural, so it cannot flake: the open happens first."""
        order: list[str] = []
        real_open, real_budget = dr._open_pdf, dr._Budget

        class RecordingBudget(real_budget):  # type: ignore[misc, valid-type]
            def __init__(self, seconds: float = 0.0) -> None:
                order.append("clock")
                super().__init__(seconds)

        monkeypatch.setattr(dr, "_open_pdf", lambda raw: (order.append("open"), real_open(raw))[1])
        monkeypatch.setattr(dr, "_Budget", RecordingBudget)
        dr._pdf_pattern(build_pdf(["alpha"]), "alpha", {"filename": "d.pdf", "format": "pdf"})
        assert order == ["open", "clock"]

    def test_a_slow_open_does_not_consume_the_scan_budget(self, monkeypatch):
        """Behavioural: an open that outlasts the whole budget still leaves the
        scan its full window. ``sleep`` only guarantees a lower bound, so the
        pre-fix failure is certain while the margin the scan needs is ~600x."""
        real_open = dr._open_pdf

        def slow_open(raw: bytes):
            time.sleep(0.5)
            return real_open(raw)

        monkeypatch.setattr(dr, "_open_pdf", slow_open)
        monkeypatch.setattr(dr, "DOCUMENT_READ_PATTERN_BUDGET_SECONDS", 0.2)
        raw = build_pdf([f"page {i} retention clause" for i in range(1, 4)])
        res = dr._pdf_pattern(raw, "retention", {"filename": "d.pdf", "format": "pdf"})
        assert "timed_out" not in res.payload
        assert res.payload["match_count"] == 3

    def test_text_pattern_splits_the_lines_before_starting_the_clock(self, monkeypatch):
        order: list[str] = []
        real_budget = dr._Budget

        class RecordingBudget(real_budget):  # type: ignore[misc, valid-type]
            def __init__(self, seconds: float = 0.0) -> None:
                order.append("clock")
                super().__init__(seconds)

        class Text(str):
            def splitlines(self, *a, **kw):  # type: ignore[override]
                order.append("split")
                return str.splitlines(self, *a, **kw)

        monkeypatch.setattr(dr, "_Budget", RecordingBudget)
        dr._text_pattern(Text("alpha\nbeta"), "alpha", {"filename": "d.txt", "format": "txt"}, "line")
        assert order == ["split", "clock"]


class TestEndToEnd:
    def test_pdf_pattern_carries_the_note_and_still_answers(self):
        raw = build_pdf(["the (a+)+$ literal lives here", "nothing on this page"])
        res = dr._pdf_pattern(raw, "(a+)+$", {"filename": "d.pdf", "format": "pdf"})
        assert res.payload["pattern_note"]
        assert res.payload["match_count"] == 1
        assert res.payload["pages_matched"] == [1]

    def test_pdf_pattern_over_a_long_document_finishes_promptly(self):
        """The regression in one assertion: a catastrophic pattern against a
        60-page document used to never return."""
        raw = build_pdf([f"SECTION {i} " + "a" * 70 for i in range(1, 61)])
        t0 = time.monotonic()
        res = dr._pdf_pattern(raw, r"(\w+\s*)+$", {"filename": "d.pdf", "format": "pdf"})
        assert time.monotonic() - t0 < 5.0
        assert res.payload["pattern_note"]

    def test_text_pattern_reports_a_timeout_with_partial_matches(self, monkeypatch):
        monkeypatch.setattr(dr, "DOCUMENT_READ_PATTERN_BUDGET_SECONDS", 1e-9)
        text = "\n".join(f"line {i} alpha" for i in range(500))
        res = dr._text_pattern(text, "alpha", {"filename": "d.txt", "format": "txt"}, "line")
        assert res.payload["timed_out"] is True
        assert "ran out of time" in res.payload["hint"]

    def test_a_normal_search_reports_no_timeout_and_no_note(self):
        raw = build_pdf([f"SECTION {i} retention clause" for i in range(1, 21)])
        res = dr._pdf_pattern(raw, "retention", {"filename": "d.pdf", "format": "pdf"})
        assert "timed_out" not in res.payload
        assert "pattern_note" not in res.payload
        assert res.payload["match_count"] == 20
