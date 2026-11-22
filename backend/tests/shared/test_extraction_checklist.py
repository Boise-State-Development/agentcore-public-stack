"""A workflow checklist call's request pieces and how a reply is read back (workflow-designer.md §22.3).

The tool and the frame are constants every checklist call puts ahead of the documents, so their
bytes are pinned here: a change is a deliberate prefix rewrite. The reply's values arrive as
strings, so the coercion and validation rules are the other half of what strict mode guarantees.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from apis.shared.extraction import checklist as cl
from apis.shared.extraction.extractor import _prompt
from apis.shared.extraction.field_sets import Field, FieldSet
from apis.shared.files.source_provenance import document_units

VERDICT = ("pass", "fail", "cant_tell")

SET = FieldSet(
    "budget-values",
    "Budget values",
    "3",
    (
        Field("total_request", "Total request", "currency", "The total amount requested."),
        Field("conferences", "Conferences per year", "number", "How many conferences a year travel covers."),
        Field("start_date", "Start date", "date", "The proposed start date.", optional=True),
        Field("cost_sharing", "Cost sharing", "boolean", "Whether cost sharing is offered."),
        Field("dmp", "Data management plan", "enum", "Whether a data management plan is included.", enum=VERDICT),
        Field("pi_name", "PI", "string", "The principal investigator's name."),
    ),
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --- The constants ---------------------------------------------------------------------------------


def test_the_tool_is_already_strict_so_strands_sends_it_as_written():
    from strands.models._strict_schema import ensure_strict_json_schema

    schema = cl.TOOL_SPEC["inputSchema"]["json"]
    assert ensure_strict_json_schema(copy.deepcopy(schema)) == schema


def test_the_tool_and_the_frame_are_pinned():
    """Both sit ahead of the documents in every checklist call's cached prefix (§22.1).

    A change here rewrites every checklist call's prefix once, at deploy: update the digest on
    purpose, never to make the test pass.
    """
    assert _digest(cl.TOOL_SPEC) == "cf82443a3b8c579d"
    assert hashlib.sha256(cl.SYSTEM_PROMPT.encode()).hexdigest()[:16] == "7500cb9c31bc43c6"


def test_the_tool_is_the_shape_that_was_measured():
    item = cl.TOOL_SPEC["inputSchema"]["json"]["properties"]["fields"]["items"]
    assert cl.TOOL_SPEC["name"] == "record_checklist"
    assert item["required"] == ["name", "value", "quote"]
    assert item["properties"]["value"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert item["properties"]["quote"]["type"] == "string"


def test_the_field_list_is_written_as_document_extract_writes_it():
    text = _prompt(SET, "body", "proposal.pdf")[0]["content"][0]["text"]
    listed = text.split("Fields to find:\n", 1)[1].split("\n\n--- BEGIN DOCUMENT", 1)[0]

    assert cl.field_list(SET) == listed
    assert "- dmp (enum): Whether a data management plan is included. One of: pass, fail, cant_tell." in listed
    assert "May legitimately be absent." in listed


def test_the_instruction_carries_the_set_the_guidance_and_the_call():
    text = cl.instruction(SET, "Read the budget justification, not the summary.")

    assert text.startswith("Checklist: Budget values (version 3)")
    assert cl.field_list(SET) in text
    assert f"{cl.GUIDANCE_HEADING}\nRead the budget justification, not the summary." in text
    assert text.endswith(cl.CALL_ONCE)


def test_no_guidance_leaves_no_heading():
    assert cl.GUIDANCE_HEADING not in cl.instruction(SET, "  ")


def test_the_instruction_is_the_same_for_the_same_set():
    assert cl.instruction(SET, "g") == cl.instruction(SET, "g")


# --- Reading a reply -------------------------------------------------------------------------------


def test_the_tool_input_is_found_in_the_reply():
    content = [
        {"text": "Recording now."},
        {"toolUse": {"toolUseId": "t1", "name": "record_checklist", "input": {"fields": []}}},
    ]
    assert cl.tool_input_of(content) == {"fields": []}
    assert cl.tool_input_of([{"text": "Total request is $598,400."}]) is None
    assert cl.tool_input_of([{"toolUse": {"name": "other", "input": {"fields": []}}}]) is None
    assert cl.tool_input_of([{"toolUse": {"name": "record_checklist", "input": {"fields": "x"}}}]) is None


@pytest.mark.parametrize(
    "field_type, raw, expected",
    [
        ("number", "3", 3.0),
        ("number", "15 pages", 15.0),
        ("number", "1,250", 1250.0),
        ("number", "3%", 3.0),
        ("number", "two or three", None),
        ("number", "1 to 2", None),
        ("currency", "$598,400", "$598,400"),
        ("currency", "about half", None),
        ("date", "March 3, 2027", "2027-03-03"),
        ("date", "2027-03-03", "2027-03-03"),
        ("date", "3rd March 2027", "2027-03-03"),
        ("date", "Fall 2027", None),
        ("boolean", "Yes", True),
        ("boolean", "no.", False),
        ("boolean", "partly", None),
        ("string", " Dr. A. Rivera ", "Dr. A. Rivera"),
        ("string", "N/A", None),
        ("string", None, None),
    ],
)
def test_values_are_coerced_by_type(field_type, raw, expected):
    field = Field("f", "F", field_type, "find it")
    assert cl.coerce_value(field, raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("pass", "pass"), ("Fail", "fail"), ("Can't tell", "cant_tell"), ("cant tell", "cant_tell"), ("maybe", None)],
)
def test_an_enum_matches_its_answers_ignoring_case_and_punctuation(raw, expected):
    field = Field("dmp", "DMP", "enum", "find it", enum=VERDICT)
    assert cl.coerce_value(field, raw) == expected


def test_a_reply_is_read_by_the_sets_rules():
    reply = {
        "fields": [
            {"name": "total_request", "value": "$598,400", "quote": "Total request: $598,400"},
            {"name": "conferences", "value": "three", "quote": "three conferences"},
            {"name": "dmp", "value": "Pass", "quote": "A Data Management Plan is included."},
            {"name": "dmp", "value": "fail", "quote": "second"},
            {"name": "invented", "value": "x", "quote": "y"},
            {"name": "cost_sharing", "value": "no", "quote": "No cost sharing is offered."},
        ]
    }

    answers = cl.read_answers(SET, reply)

    assert list(answers) == [field.name for field in SET.fields]
    assert answers["total_request"] == {"value": "$598,400", "quote": "Total request: $598,400"}
    # A value that doesn't coerce is null; its quote stays for the provenance check.
    assert answers["conferences"] == {"value": None, "quote": "three conferences"}
    # The first entry for a name wins.
    assert answers["dmp"] == {"value": "pass", "quote": "A Data Management Plan is included."}
    assert answers["cost_sharing"]["value"] is False
    # A field the reply leaves out is null with an empty quote; an unknown name is dropped.
    assert answers["start_date"] == {"value": None, "quote": ""}
    assert answers["pi_name"] == {"value": None, "quote": ""}
    assert "invented" not in answers


def test_the_answers_validate_against_the_sets_own_model():
    from apis.shared.extraction.schema import model_for_field_set

    answers = cl.read_answers(SET, {"fields": [{"name": "conferences", "value": "2", "quote": "2"}]})
    model_for_field_set(SET).model_validate(answers)
    assert answers["conferences"]["value"] == 2.0


# --- Provenance and the answer ---------------------------------------------------------------------


def _units(*pages: str):
    return document_units("pdf", list(pages))


def test_each_value_is_judged_against_the_document_that_holds_its_quote():
    answers = cl.read_answers(
        SET,
        {
            "fields": [
                {"name": "total_request", "value": "$598,400", "quote": "The total request is $598,400."},
                {"name": "pi_name", "value": "Dr. Rivera", "quote": "Principal investigator: Dr. Rivera"},
                {"name": "dmp", "value": "pass", "quote": "A Data Management Plan is included."},
                {"name": "conferences", "value": "5", "quote": "travel to two conferences a year"},
            ]
        },
    )
    budget = _units("Cover page.", "The total request is $598,400.\nWe travel to two conferences a year.")
    letter = _units("Principal investigator: Dr. Rivera", "A Data Management Plan is included.")

    rows = {row.field.name: row for row in cl.verified_rows(SET, answers, [budget, letter])}

    assert rows["total_request"].state == "supported"
    assert rows["total_request"].verification.locator.value == 2
    assert rows["pi_name"].state == "supported"
    assert rows["dmp"].state == "unassessed"  # a judgement: located, never value-checked
    assert rows["conferences"].state == "quote_unsupported"  # the passage is real, the value is not in it
    assert rows["start_date"].state == "unverified"


def test_no_readable_document_leaves_every_quote_unverified():
    answers = cl.read_answers(SET, {"fields": [{"name": "pi_name", "value": "Dr. Rivera", "quote": "Dr. Rivera"}]})
    rows = cl.verified_rows(SET, answers, [None])
    assert {row.state for row in rows} == {"unverified"}


def test_row_fields_are_the_display_rows_with_null_for_not_found():
    answers = cl.read_answers(SET, {"fields": [{"name": "pi_name", "value": "Dr. Rivera", "quote": "PI: Dr. Rivera"}]})
    fields = cl.row_fields(cl.verified_rows(SET, answers, [_units("PI: Dr. Rivera")]))

    by_name = {f["name"]: f for f in fields}
    assert by_name["pi_name"]["value"] == "Dr. Rivera"
    assert by_name["pi_name"]["quote"] == "PI: Dr. Rivera"
    assert by_name["pi_name"]["locator"] == {"kind": "page", "value": 1, "precision": "measured"}
    assert by_name["total_request"]["value"] is None


def test_the_answer_is_one_table_per_set_with_values_and_states_only():
    answers = cl.read_answers(
        SET, {"fields": [{"name": "total_request", "value": "$598,400", "quote": "Total | $598,400"}]}
    )
    rows = tuple(cl.verified_rows(SET, answers, [_units("Total | $598,400")]))
    text = cl.render_answer(
        [
            cl.SetResult(name=SET.name, title=SET.title, status="done", field_set=SET, rows=rows),
            cl.SetResult(name="compliance-plans", title="compliance-plans", status="error", error="no_tool_call"),
        ]
    )

    assert text.startswith("### Budget values\n\n| Field | Value | State |\n|---|---|---|")
    assert "| Total request | $598,400 | supported |" in text
    assert "| PI | not found | unverified |" in text  # no quote: nothing was traced
    assert "Total \\| $598,400" not in text  # the quote is display, never in the answer
    assert "### compliance-plans\n\nNot filled in: the model did not record an answer, twice." in text


def test_a_set_too_long_to_carry_its_values_keeps_names_and_states():
    long_value = "x" * 190
    fields = tuple(Field(f"f{i}", f"Field {i}", "string", "find") for i in range(14))
    wide = FieldSet("wide", "Wide", "1", fields)
    answers = cl.read_answers(wide, {"fields": [{"name": f.name, "value": long_value, "quote": ""} for f in fields]})
    text = cl.render_set(
        cl.SetResult(name="wide", title="Wide", status="done", field_set=wide, rows=tuple(cl.verified_rows(wide, answers, [None])))
    )

    assert "| Field | State |" in text
    assert long_value not in text


def test_a_list_field_is_unsupported_never_coerced():
    listed = FieldSet("aims", "Aims", "1", (Field("aims", "Aims", "list", "The aims."), Field("pi", "PI", "string", "The PI.")))
    assert cl.unsupported_fields(listed) == ["aims"]
    assert cl.unsupported_fields(SET) == []
