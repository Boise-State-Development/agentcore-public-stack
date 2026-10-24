"""Rows carry the verdict they were given, and nothing stronger.

``docs/specs/structured-extraction.md`` §5.1, §7, D7, D8, D9. These are the
tests that matter most, because the failure they guard against is the one that
reads as success: a tidy table of twenty values where every badge says
``supported`` because nothing is actually being checked.

Everything here runs against the real ``source_provenance`` module. Mocking it
would defeat the point — the property under test is that this code does not
decide a state, and a stubbed verifier is a state decided in the test.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from apis.shared.extraction.field_sets import MAX_FIELDS, parse_field_set
from apis.shared.extraction.rows import (
    MAX_RESULT_CHARS,
    MAX_VALUE_CHARS,
    build_claims,
    build_rows,
    event_fields,
    model_facing_result,
    state_counts,
)
from apis.shared.files.source_provenance import (
    METHOD_EMPTY_VALUE,
    METHOD_ENUM_FIELD,
    METHOD_NO_QUOTE,
    METHOD_QUOTE_NOT_LOCATED,
    PRECISION_MEASURED,
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
    document_units,
)

SET = """---
title: NSF Solicitation
version: 2
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| deadline | Submission deadline | date | the full proposal deadline | | no |
| budget_cap | Budget cap | currency | the maximum total award | | no |
| cost_share | Cost sharing | enum | whether cost sharing is required | required, not required | no |
| page_limit | Page limit | number | the page limit | | yes |
"""

PAGE_ONE = (
    "Program Solicitation NSF 26-501.\n"
    "Full proposals are due March 3, 2026, by 5:00 p.m. submitter's local time."
)
PAGE_TWO = (
    "Awards will be made for up to $500,000 in total costs over three years.\n"
    "No cost sharing is required under this solicitation.\n"
    "The Project Description is limited to 15 pages."
)


@pytest.fixture
def field_set():
    return parse_field_set(SET, name="nsf-solicitation")


@pytest.fixture
def document():
    return document_units("pdf", [PAGE_ONE, PAGE_TWO], precision=PRECISION_MEASURED)


def extracted(**overrides: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """A well-formed extraction, with per-field overrides."""
    base = {
        "deadline": {
            "value": "2026-03-03",
            "quote": "Full proposals are due March 3, 2026",
        },
        "budget_cap": {
            "value": "$500,000",
            "quote": "Awards will be made for up to $500,000 in total costs",
        },
        "cost_share": {
            "value": "not required",
            "quote": "No cost sharing is required under this solicitation.",
        },
        "page_limit": {
            "value": 15.0,
            "quote": "The Project Description is limited to 15 pages.",
        },
    }
    base.update(overrides)
    return base


class TestTheVerdictComesFromTheCheck:
    def test_a_value_in_its_cited_passage_is_supported(self, field_set, document) -> None:
        rows = {r.field.name: r for r in build_rows(field_set, extracted(), document)}
        assert rows["budget_cap"].state == STATE_SUPPORTED
        assert rows["budget_cap"].locator_text == "p. 2"

    def test_a_fabricated_value_with_a_real_passage_is_the_hallucination_signal(
        self, field_set, document
    ) -> None:
        """The case the whole feature exists for.

        The quote is a genuine sentence from the document; the value is not in
        it. A check that only asked "is this passage real?" would badge this
        ``supported``.
        """
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(
                    budget_cap={
                        "value": "$750,000",
                        "quote": "Awards will be made for up to $500,000 in total costs",
                    }
                ),
                document,
            )
        }
        assert rows["budget_cap"].state == STATE_QUOTE_UNSUPPORTED
        # It still has a locator — the passage IS on page 2. That is exactly
        # why the state has to be read and not inferred from the locator.
        assert rows["budget_cap"].locator_text == "p. 2"

    def test_a_quote_not_in_the_document_is_unverified(self, field_set, document) -> None:
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(
                    budget_cap={
                        "value": "$500,000",
                        "quote": "The ceiling for this program is five hundred thousand dollars.",
                    }
                ),
                document,
            )
        }
        assert rows["budget_cap"].state == STATE_UNVERIFIED
        assert rows["budget_cap"].verification.method == METHOD_QUOTE_NOT_LOCATED
        assert rows["budget_cap"].locator_text is None

    def test_a_value_with_no_quote_is_unverified_not_supported(
        self, field_set, document
    ) -> None:
        """D9: nothing inherits the strongest badge."""
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(budget_cap={"value": "$500,000", "quote": None}),
                document,
            )
        }
        assert rows["budget_cap"].state == STATE_UNVERIFIED
        assert rows["budget_cap"].verification.method == METHOD_NO_QUOTE

    def test_an_absent_value_is_unassessed_not_a_failure(
        self, field_set, document
    ) -> None:
        """A document that does not state a field is answering, not failing."""
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(page_limit={"value": None, "quote": None}),
                document,
            )
        }
        assert rows["page_limit"].state == STATE_UNVERIFIED

    def test_a_null_value_with_a_real_quote_is_unassessed(
        self, field_set, document
    ) -> None:
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(
                    page_limit={
                        "value": None,
                        "quote": "The Project Description is limited to 15 pages.",
                    }
                ),
                document,
            )
        }
        assert rows["page_limit"].state == STATE_UNASSESSED
        assert rows["page_limit"].verification.method == METHOD_EMPTY_VALUE

    def test_an_unreadable_document_verifies_nothing(self, field_set) -> None:
        """A scanned PDF yields no units. Every row says so, rather than the
        code inventing a document to search (§15, G3)."""
        rows = build_rows(field_set, extracted(), None)
        assert {r.state for r in rows} == {STATE_UNVERIFIED}

    def test_a_straddling_quote_is_hedged_not_dropped(self, field_set) -> None:
        """A passage spanning a page break exists in neither page alone. The
        source module reports the page it starts on, marked estimated — never
        ``unverified``, which would be a manufactured hallucination signal."""
        document = document_units(
            "pdf", ["the deadline is", "March 3, 2026 at 5pm"], precision=PRECISION_MEASURED
        )
        rows = {
            r.field.name: r
            for r in build_rows(
                field_set,
                extracted(
                    deadline={
                        "value": "March 3, 2026",
                        "quote": "the deadline is\nMarch 3, 2026",
                    }
                ),
                document,
            )
        }
        assert rows["deadline"].state == STATE_SUPPORTED
        assert rows["deadline"].locator_text == "p. ~1"


class TestEnumFieldIsDerivedFromTheSchema:
    """D7 — the one genuine improvement over ``verify_sources``, and it is free."""

    def test_an_enum_field_is_exempted_from_the_value_check_by_its_type(
        self, field_set, document
    ) -> None:
        rows = {r.field.name: r for r in build_rows(field_set, extracted(), document)}
        assert rows["cost_share"].state == STATE_UNASSESSED
        assert rows["cost_share"].verification.method == METHOD_ENUM_FIELD

    def test_the_model_cannot_opt_a_plain_field_into_that_exemption(
        self, field_set
    ) -> None:
        """``verify_sources`` takes ``from_fixed_set`` from the model, which
        lets it exempt its own answer from the value check. Here the field set
        declares the type, so there is nothing to assert."""
        claims = {c.claim_id: c for c in build_claims(field_set, extracted())}
        assert claims["cost_share"].enum_field is True
        assert claims["budget_cap"].enum_field is False
        assert claims["deadline"].enum_field is False

    def test_a_boolean_field_is_a_judgement_too(self) -> None:
        field_set = parse_field_set(
            "| name | type | find |\n|---|---|---|\n"
            "| allows_foreign | boolean | whether a foreign PI may lead |\n",
            name="s",
        )
        claims = build_claims(
            field_set, {"allows_foreign": {"value": False, "quote": "x"}}
        )
        assert claims[0].enum_field is True


class TestWhatTheModelIsTold:
    """§5.1, D8. The values go in; the provenance does not."""

    def test_values_and_states_reach_the_model(self, field_set, document) -> None:
        result = model_facing_result(
            build_rows(field_set, extracted(), document),
            field_set=field_set,
            artifact_id="abc123",
            schema_enforced=True,
        )
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["budget_cap"]["value"] == "$500,000"
        assert by_name["budget_cap"]["state"] == STATE_SUPPORTED

    def test_no_provenance_detail_reaches_the_content_blocks(
        self, field_set, document
    ) -> None:
        """The cost rule: a tool result IS the Converse payload and IS in the
        cacheable prefix, so a display string there is paid at model rates on
        every later turn, forever."""
        result = model_facing_result(
            build_rows(field_set, extracted(), document),
            field_set=field_set,
            artifact_id="abc123",
            schema_enforced=True,
        )
        serialized = json.dumps(result)
        for leaked in (
            "Awards will be made",          # a quote
            "p. 2",                          # a rendered locator
            PRECISION_MEASURED,              # precision
            METHOD_ENUM_FIELD,               # method
        ):
            assert leaked not in serialized, f"{leaked!r} must stay on the side channel"
        for field in result["fields"]:
            assert set(field) == {"name", "value", "state"}

    def test_an_unenforced_run_is_disclosed_to_the_model(
        self, field_set, document
    ) -> None:
        result = model_facing_result(
            build_rows(field_set, extracted(), document),
            field_set=field_set,
            artifact_id=None,
            schema_enforced=False,
        )
        assert result["schemaEnforced"] is False

    def test_an_enforced_run_adds_no_key_for_it(self, field_set, document) -> None:
        """Absence is the common case, and a key present on every result is a
        key paid for on every later turn."""
        result = model_facing_result(
            build_rows(field_set, extracted(), document),
            field_set=field_set,
            artifact_id=None,
            schema_enforced=True,
        )
        assert "schemaEnforced" not in result

    def test_a_long_value_is_truncated(self, field_set, document) -> None:
        long = "x" * (MAX_VALUE_CHARS + 500)
        result = model_facing_result(
            build_rows(
                field_set,
                extracted(budget_cap={"value": long, "quote": "Awards will be made"}),
                document,
            ),
            field_set=field_set,
            artifact_id=None,
            schema_enforced=True,
        )
        value = next(f for f in result["fields"] if f["name"] == "budget_cap")["value"]
        assert len(value) <= MAX_VALUE_CHARS
        assert value.endswith("…")

    def test_an_oversized_result_drops_to_names_and_states(self) -> None:
        """§5.1's second bound. The states are what the model needs to act;
        the values are on screen in the table either way."""
        # A full-width set (the measured Bedrock ceiling) whose values are each
        # at the per-value cap: 14 x 200 chars is well past the 2,000-char
        # result bound, so the drop has to fire.
        rows_md = "".join(
            f"| field_{i} | the value for field {i} |\n" for i in range(MAX_FIELDS)
        )
        field_set = parse_field_set(f"| name | find |\n|---|---|\n{rows_md}", name="big")
        huge = {
            f"field_{i}": {"value": "y" * MAX_VALUE_CHARS, "quote": None}
            for i in range(MAX_FIELDS)
        }
        result = model_facing_result(
            build_rows(field_set, huge, None),
            field_set=field_set,
            artifact_id=None,
            schema_enforced=True,
        )
        assert result["valuesOmitted"] is True
        assert all(set(f) == {"name", "state"} for f in result["fields"])
        assert len(json.dumps(result)) < MAX_RESULT_CHARS * 2

    def test_a_list_value_is_joined_for_the_model(self) -> None:
        field_set = parse_field_set(
            "| name | type | find |\n|---|---|---|\n| topics | list | the topics |\n",
            name="s",
        )
        result = model_facing_result(
            build_rows(field_set, {"topics": {"value": ["a", "b"], "quote": None}}, None),
            field_set=field_set,
            artifact_id=None,
            schema_enforced=True,
        )
        assert result["fields"][0]["value"] == "a, b"


class TestTheSideChannelPayload:
    def test_the_event_carries_everything_the_model_is_not_told(
        self, field_set, document
    ) -> None:
        fields = {f["name"]: f for f in event_fields(build_rows(field_set, extracted(), document))}
        budget = fields["budget_cap"]
        assert budget["quote"].startswith("Awards will be made")
        assert budget["locator"] == {"kind": "page", "value": 2, "precision": "measured"}
        assert budget["method"]
        assert budget["title"] == "Budget cap"
        assert budget["type"] == "currency"

    def test_precision_always_travels_with_a_locator(self, field_set, document) -> None:
        """A reader who cannot tell an estimated position from a measured one
        has been given false confidence."""
        for field in event_fields(build_rows(field_set, extracted(), document)):
            if field["locator"] is not None:
                assert field["locator"]["precision"]

    def test_state_counts_are_read_not_computed(self, field_set, document) -> None:
        """The honesty readout (§14): if every row of every run is
        ``supported``, the integration is not working."""
        counts = state_counts(build_rows(field_set, extracted(), document))
        # deadline, budget_cap and page_limit — the last one matched
        # numerically, 15.0 against "limited to 15 pages", which is the value
        # check doing its job rather than a string compare getting lucky.
        assert counts[STATE_SUPPORTED] == 3
        assert counts[STATE_UNASSESSED] == 1      # cost_share, an enum (D7)
        assert sum(counts.values()) == 4


class TestOrder:
    def test_rows_follow_the_authored_field_order(self, field_set, document) -> None:
        """Not the order the model emitted, and not a sort (D4)."""
        shuffled = dict(reversed(list(extracted().items())))
        rows = build_rows(field_set, shuffled, document)
        assert [r.field.name for r in rows] == [f.name for f in field_set.fields]

    def test_a_field_the_model_omitted_entirely_still_produces_a_row(
        self, field_set, document
    ) -> None:
        """The schema makes this impossible, but a row silently missing from a
        reviewer's table would be worse than one marked unverified."""
        partial = extracted()
        partial.pop("page_limit")
        rows = build_rows(field_set, partial, document)
        assert len(rows) == 4
        assert rows[-1].state == STATE_UNVERIFIED


class TestNumberRendering:
    """A whole-number float must not read as ``15.0``.

    Found on a real run, not in a fixture: JSON has one number type, so a
    ``number`` field whose document says "limited to 15 pages" comes back as
    ``15.0`` and rendered that way in a column headed "Page limit" — which
    reads as a bug to the office looking at it, and is not what the document
    said.
    """

    @pytest.fixture
    def numeric_set(self):
        return parse_field_set(
            "| name | title | type | find |\n|---|---|---|---|\n"
            "| page_limit | Page limit | number | the page limit |\n"
            "| indirect_rate | Indirect rate | number | the rate |\n",
            name="s",
        )

    def test_a_whole_number_loses_its_decimal(self, numeric_set) -> None:
        rows = build_rows(
            numeric_set,
            {
                "page_limit": {"value": 15.0, "quote": "limited to 15 pages"},
                "indirect_rate": {"value": 54.5, "quote": "a rate of 54.5%"},
            },
            None,
        )
        fields = {f["name"]: f["value"] for f in event_fields(rows)}
        assert fields["page_limit"] == "15"
        assert fields["indirect_rate"] == "54.5"

    def test_the_model_facing_value_is_shortened_too(self, numeric_set) -> None:
        rows = build_rows(
            numeric_set,
            {
                "page_limit": {"value": 15.0, "quote": None},
                "indirect_rate": {"value": 54.5, "quote": None},
            },
            None,
        )
        result = model_facing_result(
            rows, field_set=numeric_set, artifact_id=None, schema_enforced=True
        )
        values = {f["name"]: f["value"] for f in result["fields"]}
        assert values["page_limit"] == "15"
        assert values["indirect_rate"] == "54.5"
