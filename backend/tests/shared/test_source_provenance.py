"""Unit tests for ``apis.shared.files.source_provenance``.

The module is pure, so every state and every ``method`` is reachable from a
literal string — no S3, no PDF, no model. The cases are grouped by the claim
each one defends, because the point of this feature is that a located quote and
a supported value are different propositions.
"""

import importlib

import pytest

from apis.shared.files import source_provenance as sp
from apis.shared.files.source_provenance import (
    METHOD_DATE,
    METHOD_DOCUMENT_UNREADABLE,
    METHOD_EMPTY_VALUE,
    METHOD_ENUM_FIELD,
    METHOD_LITERAL,
    METHOD_NO_MATCH,
    METHOD_NO_QUOTE,
    METHOD_NON_SCALAR,
    METHOD_NUMERIC,
    METHOD_QUOTE_NOT_LOCATED,
    METHOD_QUOTE_TOO_LONG,
    PRECISION_ESTIMATED,
    PRECISION_MEASURED,
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
    Claim,
    DocumentUnits,
    document_units,
    render_locator,
    verify_claim,
    verify_claims,
)

# A two-page award letter. Page 1 carries the amount, page 2 the dates.
PAGE_1 = (
    "NOTICE OF AWARD\n"
    "The total approved budget for this project is $487,500 for the period of "
    "performance.\n"
    "Indirect costs are calculated at 54% of MTDC."
)
PAGE_2 = (
    "The project period begins January 15, 2026 and ends December 31, 2028.\n"
    "Cost sharing is required for this award.\n"
    "Direct costs total $316,200."
)


def award_letter(precision: str = PRECISION_MEASURED) -> DocumentUnits:
    return document_units("pdf", [PAGE_1, PAGE_2], precision=precision)


def claim(value, quote, *, enum_field: bool = False, claim_id: str = "c1") -> Claim:
    return Claim(claim_id=claim_id, value=value, quote=quote, enum_field=enum_field)


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------


class TestFourStates:
    def test_supported_when_value_is_in_a_located_quote(self):
        result = verify_claim(
            claim("$487,500", "The total approved budget for this project is $487,500"),
            award_letter(),
        )
        assert result.state == STATE_SUPPORTED
        assert result.method == METHOD_LITERAL
        assert result.locator.kind == "page"
        assert result.locator.value == 1
        assert result.locator.precision == PRECISION_MEASURED

    def test_quote_unsupported_is_the_hallucination_signal(self):
        """A real sentence from the budget section, an invented amount.

        This is the case the whole feature exists for: question 1 passes, so a
        badge keyed on "did we find the quote" would certify it.
        """
        result = verify_claim(
            claim("$912,000", "The total approved budget for this project is $487,500"),
            award_letter(),
        )
        assert result.state == STATE_QUOTE_UNSUPPORTED
        assert result.method == METHOD_NO_MATCH
        # Still located — the passage is real, which is exactly the trap.
        assert result.locator is not None
        assert result.locator.value == 1

    def test_unassessed_when_the_value_question_does_not_apply(self):
        result = verify_claim(
            claim("Yes", "Cost sharing is required for this award.", enum_field=True),
            award_letter(),
        )
        assert result.state == STATE_UNASSESSED
        assert result.method == METHOD_ENUM_FIELD
        assert result.locator is not None

    def test_unverified_when_the_quote_is_not_in_the_document(self):
        result = verify_claim(
            claim("$487,500", "The awarded sum shall not exceed four hundred thousand"),
            award_letter(),
        )
        assert result.state == STATE_UNVERIFIED
        assert result.method == METHOD_QUOTE_NOT_LOCATED
        assert result.locator is None

    def test_unverified_when_no_quote_was_given(self):
        for empty in (None, "", "   "):
            result = verify_claim(claim("$487,500", empty), award_letter())
            assert result.state == STATE_UNVERIFIED
            assert result.method == METHOD_NO_QUOTE

    def test_unverified_when_the_document_has_no_text_layer(self):
        """A scanned PDF. Nothing is traced, and the method says why."""
        scanned = document_units("pdf", ["", "", ""])
        result = verify_claim(claim("$487,500", "any passage at all"), scanned)
        assert result.state == STATE_UNVERIFIED
        assert result.method == METHOD_DOCUMENT_UNREADABLE

    def test_unverified_when_there_is_no_document_at_all(self):
        result = verify_claim(claim("1", "a quote"), None)
        assert result.state == STATE_UNVERIFIED
        assert result.method == METHOD_DOCUMENT_UNREADABLE


class TestQuestionOrder:
    def test_an_unlocated_quote_is_never_promoted_even_when_it_holds_the_value(self):
        """A fabricated passage containing the value proves nothing.

        The value check must not run before the quote is known to exist, or a
        model could earn ``supported`` by quoting itself.
        """
        result = verify_claim(
            claim("$487,500", "We hereby award $487,500 under this invented sentence."),
            award_letter(),
        )
        assert result.state == STATE_UNVERIFIED
        assert result.method == METHOD_QUOTE_NOT_LOCATED


class TestNoPublicStateConstructor:
    def test_there_is_no_function_taking_the_two_booleans(self):
        """The ported design exposed ``support_state(verified, value_supported)``.

        That signature is the misuse vector: a caller can pass a pair it made
        up, or pass the weaker answer twice. Nothing like it may reappear.
        """
        assert not hasattr(sp, "support_state")
        assert not hasattr(sp, "support_state_of")

    def test_the_result_carries_no_booleans_to_re_derive_from(self):
        result = verify_claim(claim("$487,500", "approved budget"), award_letter())
        for forbidden in ("verified", "value_supported", "quote_located", "value_in_quote"):
            assert not hasattr(result, forbidden)

    def test_the_public_surface_is_the_documented_one(self):
        assert set(sp.__all__) >= {
            "verify_claim",
            "verify_claims",
            "document_units",
            "render_locator",
        }


# ---------------------------------------------------------------------------
# Unicode normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.parametrize(
        "quote",
        [
            # Verbatim.
            "The total approved budget for this project is $487,500",
            # NBSP where the document has a plain space.
            "The total approved budget for this project is $487,500",
            # Collapsed whitespace.
            "The  total   approved    budget for this project is $487,500",
            # Case.
            "the TOTAL approved BUDGET for this project is $487,500",
            # Zero-width characters the model inserted.
            "The total​ approved budget for this project is $487,500",
            # A soft hyphen, as a hyphenating PDF leaves behind.
            "The to­tal approved budget for this project is $487,500",
        ],
    )
    def test_quotes_differing_only_by_folding_still_locate(self, quote):
        result = verify_claim(claim("$487,500", quote), award_letter())
        assert result.state == STATE_SUPPORTED, quote

    def test_curly_apostrophe_in_the_document_matches_a_straight_one(self):
        doc = document_units("txt", ["The sponsor’s share is $10,000."])
        result = verify_claim(claim("$10,000", "The sponsor's share is $10,000."), doc)
        assert result.state == STATE_SUPPORTED

    def test_curly_double_quotes_in_the_document_match_straight_ones(self):
        doc = document_units("txt", ["Marked “Final” on 3 copies."])
        result = verify_claim(claim("3", 'Marked "Final" on 3 copies.'), doc)
        assert result.state == STATE_SUPPORTED

    def test_characters_the_model_added_are_not_folded_away(self):
        """The contract is a *verbatim* passage. Punctuation the document does
        not contain makes the quote something the model composed, and reporting
        it as located would certify the composition."""
        result = verify_claim(
            claim("$487,500", '"The total approved budget" for this project is $487,500'),
            award_letter(),
        )
        assert result.state == STATE_UNVERIFIED
        assert result.method == METHOD_QUOTE_NOT_LOCATED

    def test_ligature_in_the_document_matches_its_letters(self):
        doc = document_units("txt", ["The ﬁnal report is due in 90 days."])
        result = verify_claim(claim("90", "The final report is due in 90 days."), doc)
        assert result.state == STATE_SUPPORTED

    def test_dash_variants_fold_together(self):
        doc = document_units("txt", ["Period: 2026–2028, 36 months."])
        result = verify_claim(claim("36", "Period: 2026-2028, 36 months."), doc)
        assert result.state == STATE_SUPPORTED

    def test_index_map_projects_back_to_real_offsets(self):
        text = "  A  B  "
        normalized, index_map = sp.normalize_with_map(text)
        assert normalized == "a b"
        assert len(index_map) == len(normalized)
        assert text[index_map[0]] == "A"
        assert text[index_map[2]] == "B"

    def test_a_quote_that_normalizes_to_nothing_does_not_locate(self):
        result = verify_claim(claim("1", "​​"), award_letter())
        assert result.state == STATE_UNVERIFIED


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------


class TestLocators:
    def test_the_second_page_resolves_to_page_two(self):
        result = verify_claim(claim("$316,200", "Direct costs total $316,200."), award_letter())
        assert result.locator.value == 2
        assert result.locator.precision == PRECISION_MEASURED

    def test_a_docx_gets_a_paragraph_not_a_page(self):
        """Nothing measured a page here, so claiming one would be invented."""
        doc = document_units("docx", ["Title", "The fee is $200.", "Signed."])
        result = verify_claim(claim("$200", "The fee is $200."), doc)
        assert result.locator.kind == "paragraph"
        assert result.locator.value == 2

    def test_a_text_file_gets_a_line(self):
        doc = document_units("md", ["# Budget", "Total: 42 units"])
        result = verify_claim(claim("42", "Total: 42 units"), doc)
        assert result.locator.kind == "line"
        assert result.locator.value == 2

    def test_a_quote_spanning_a_page_break_locates_at_its_first_page(self):
        """It exists in neither page alone.

        Searching page by page would report ``unverified`` — a hallucination
        signal this module manufactured, which is the one error it must not
        make.
        """
        doc = document_units("pdf", ["...ends with this sentence", "and continues here..."])
        result = verify_claim(
            claim("1", "ends with this sentence and continues here"), doc
        )
        assert result.state != STATE_UNVERIFIED
        assert result.locator.value == 1

    def test_a_straddling_match_is_marked_estimated(self):
        """One number for a passage that occupies two pages is a convention."""
        doc = document_units("pdf", ["...ends with this sentence", "and continues here..."])
        result = verify_claim(
            claim("1", "ends with this sentence and continues here"), doc
        )
        assert result.locator.precision == PRECISION_ESTIMATED

    def test_a_match_inside_one_unit_keeps_the_documents_precision(self):
        result = verify_claim(claim("54%", "Indirect costs are calculated at 54% of MTDC."), award_letter())
        assert result.locator.precision == PRECISION_MEASURED

    def test_an_estimated_document_never_yields_a_measured_locator(self):
        """A managed-KB chunk page, or OCR with no page structure."""
        doc = award_letter(precision=PRECISION_ESTIMATED)
        result = verify_claim(claim("$316,200", "Direct costs total $316,200."), doc)
        assert result.locator.precision == PRECISION_ESTIMATED

    def test_precision_must_be_stated_explicitly(self):
        """An optional flag whose absence reads as exact is how a guess gets
        rendered as ``p. 234``."""
        with pytest.raises(ValueError, match="precision"):
            DocumentUnits("page", ["a"], precision="probably")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="kind"):
            DocumentUnits("sheet", ["a"], precision=PRECISION_MEASURED)

    def test_an_unknown_format_has_no_unit(self):
        with pytest.raises(ValueError, match="locator unit"):
            document_units("xlsx", ["a"])

    def test_payload_always_carries_precision(self):
        result = verify_claim(claim("$316,200", "Direct costs total $316,200."), award_letter())
        assert result.locator.to_payload() == {
            "kind": "page",
            "value": 2,
            "precision": PRECISION_MEASURED,
        }


class TestRendering:
    @pytest.mark.parametrize(
        "kind,precision,expected",
        [
            ("page", PRECISION_MEASURED, "p. 12"),
            ("page", PRECISION_ESTIMATED, "p. ~12"),
            ("paragraph", PRECISION_MEASURED, "¶ 12"),
            ("paragraph", PRECISION_ESTIMATED, "¶ ~12"),
            ("line", PRECISION_MEASURED, "line 12"),
            ("line", PRECISION_ESTIMATED, "line ~12"),
        ],
    )
    def test_renders_its_own_unit_and_hedges_an_estimate(self, kind, precision, expected):
        assert render_locator(sp.Locator(kind=kind, value=12, precision=precision)) == expected

    def test_nothing_to_cite_renders_nothing(self):
        """So a caller falls back to the bare document name rather than
        printing a position it does not have."""
        assert render_locator(None) is None


# ---------------------------------------------------------------------------
# The value check: over-claiming is the failure direction
# ---------------------------------------------------------------------------


class TestValueBoundaries:
    @pytest.mark.parametrize(
        "value,passage",
        [
            # A fragment of a longer number.
            ("500000", "The ceiling is 5000000 dollars."),
            ("$500,000", "The ceiling is $500,000.99 exactly."),
            ("2026", "Reference 12026 applies."),
            # A fragment of a longer name.
            ("John Smith", "Submitted by John Smithson, PI."),
            ("Chem", "Department of Chemistry."),
        ],
    )
    def test_a_fragment_does_not_support_a_value(self, value, passage):
        doc = document_units("txt", [passage])
        result = verify_claim(claim(value, passage), doc)
        assert result.state == STATE_QUOTE_UNSUPPORTED, (value, passage)
        assert result.method == METHOD_NO_MATCH

    def test_a_later_whole_match_counts_even_when_the_first_is_a_fragment(self):
        passage = "Phase one is $1,500,000 and phase two is $500,000."
        doc = document_units("txt", [passage])
        result = verify_claim(claim("$500,000", passage), doc)
        assert result.state == STATE_SUPPORTED

    def test_sentence_punctuation_still_ends_a_value(self):
        passage = "The award is 487500. No further funds are committed."
        doc = document_units("txt", [passage])
        result = verify_claim(claim("487500", passage), doc)
        assert result.state == STATE_SUPPORTED


class TestNumericAndDateEquivalence:
    def test_formatting_differences_still_support(self):
        passage = "The total approved budget for this project is $487,500 for the period of"
        result = verify_claim(claim(487500, passage), award_letter())
        assert result.state == STATE_SUPPORTED
        assert result.method == METHOD_NUMERIC

    def test_a_percentage_is_not_the_bare_number(self):
        """``$50`` must not be certified by "50% of MTDC"."""
        passage = "Indirect costs are calculated at 54% of MTDC."
        doc = document_units("txt", [passage])
        assert verify_claim(claim("$54", passage), doc).state == STATE_QUOTE_UNSUPPORTED
        assert verify_claim(claim("54%", passage), doc).state == STATE_SUPPORTED

    def test_a_bare_number_is_not_a_percentage(self):
        passage = "A fee of 54 dollars applies."
        doc = document_units("txt", [passage])
        assert verify_claim(claim("54%", passage), doc).state == STATE_QUOTE_UNSUPPORTED

    def test_comma_groups_only_count_in_thousands_position(self):
        """A permissive pattern swallows "sections 1, 2 and 3" and parses 123."""
        passage = "See sections 1, 2 and 3 of the terms."
        doc = document_units("txt", [passage])
        assert verify_claim(claim("123", passage), doc).state == STATE_QUOTE_UNSUPPORTED

    @pytest.mark.parametrize(
        "value",
        ["2026-01-15", "01/15/2026", "January 15, 2026", "15 January 2026"],
    )
    def test_dates_match_across_written_forms(self, value):
        result = verify_claim(
            claim(value, "The project period begins January 15, 2026 and ends December 31, 2028."),
            award_letter(),
        )
        assert result.state == STATE_SUPPORTED
        assert result.method in (METHOD_DATE, METHOD_LITERAL)

    def test_a_different_date_in_the_same_passage_is_unsupported(self):
        result = verify_claim(
            claim("2027-06-01", "The project period begins January 15, 2026 and ends December 31, 2028."),
            award_letter(),
        )
        assert result.state == STATE_QUOTE_UNSUPPORTED


class TestUnassessedReasons:
    def test_a_not_found_sentinel_has_nothing_to_support(self):
        for sentinel in ("N/A", "none", "not specified", "", "  ", None):
            result = verify_claim(
                claim(sentinel, "Cost sharing is required for this award."), award_letter()
            )
            assert result.state == STATE_UNASSESSED, sentinel
            assert result.method == METHOD_EMPTY_VALUE

    @pytest.mark.parametrize("value", [True, False, ["a"], {"a": 1}, (1, 2)])
    def test_a_non_scalar_has_no_verbatim_form(self, value):
        result = verify_claim(
            claim(value, "Cost sharing is required for this award."), award_letter()
        )
        assert result.state == STATE_UNASSESSED
        assert result.method == METHOD_NON_SCALAR

    def test_a_quote_longer_than_a_passage_is_not_scanned(self):
        """Model-controlled input, and the date scan backtracks badly on it."""
        long_page = "x" * 5000
        doc = document_units("pdf", [long_page])
        result = verify_claim(claim("42", long_page), doc)
        assert result.state == STATE_UNASSESSED
        assert result.method == METHOD_QUOTE_TOO_LONG
        # Located all the same: traceable, just not confirmable.
        assert result.locator is not None


class TestKnownWeaknesses:
    """Recorded as tests so a future change cannot quietly claim more.

    Each of these is a documented over-read of ``supported``; the point is that
    the module's behaviour here is deliberate and measured, not that it is
    desirable.
    """

    def test_the_check_does_not_know_which_field_a_passage_assigns(self):
        passage = "Indirect costs total $316,200 under this award."
        doc = document_units("txt", [passage])
        # Asked as a *direct* cost, and still supported: the number is in the
        # passage. "Supported" means exactly that and no more.
        assert verify_claim(claim("$316,200", passage), doc).state == STATE_SUPPORTED

    def test_a_judgment_about_the_passage_reads_unsupported_without_an_enum(self):
        passage = "Cost sharing is required for this award."
        doc = document_units("txt", [passage])
        assert verify_claim(claim("Yes", passage), doc).state == STATE_QUOTE_UNSUPPORTED
        assert (
            verify_claim(claim("Yes", passage, enum_field=True), doc).state
            == STATE_UNASSESSED
        )

    def test_a_multi_part_value_assembled_from_a_sentence_reads_unsupported(self):
        passage = "Submitted by Jane Smith of the Chemistry department."
        doc = document_units("txt", [passage])
        assert (
            verify_claim(claim("Jane Smith, Chemistry", passage), doc).state
            == STATE_QUOTE_UNSUPPORTED
        )


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class TestVerifyClaims:
    def test_a_batch_keeps_its_ids_and_order(self):
        doc = award_letter()
        results = verify_claims(
            [
                claim("$487,500", "approved budget for this project is $487,500", claim_id="amount"),
                claim("$912,000", "approved budget for this project is $487,500", claim_id="fake"),
                claim("x", "nowhere in the document", claim_id="missing"),
            ],
            doc,
        )
        assert [r.claim_id for r in results] == ["amount", "fake", "missing"]
        assert [r.state for r in results] == [
            STATE_SUPPORTED,
            STATE_QUOTE_UNSUPPORTED,
            STATE_UNVERIFIED,
        ]

    def test_an_empty_batch_is_an_empty_list(self):
        assert verify_claims([], award_letter()) == []


# ---------------------------------------------------------------------------
# Purity, and agreement with the paths that produce the units
# ---------------------------------------------------------------------------


def test_the_module_imports_nothing_from_apis_shared():
    """What makes it safe to import from app-api, the Runtime and a Lambda.

    Read from the source rather than from ``sys.modules``: an import pulled in
    by another test would not show up there, and an import added here that a
    reviewer overlooks is exactly what this guards.
    """
    from pathlib import Path

    source = Path(sp.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "apis." not in stripped, f"non-pure import: {stripped}"
            assert "boto" not in stripped, f"non-pure import: {stripped}"


def test_unit_kinds_agree_with_the_prepared_text_layer():
    """``UNIT_KINDS`` is a duplicate of ``large_document.UNIT_FOR_FORMAT``, kept
    so this module imports nothing. If the two drift, a prepared PDF's page
    records would be read as something else."""
    large_document = importlib.import_module("apis.shared.files.large_document")
    assert sp.UNIT_KINDS == large_document.UNIT_FOR_FORMAT
