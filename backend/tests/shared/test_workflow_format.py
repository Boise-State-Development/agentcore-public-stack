"""The saved-workflow file format (``docs/specs/workflow-designer.md`` §9.1, PR-D.1).

What these pin, in order of what would hurt: a file reads back as itself and re-serializes
byte-for-byte; every envelope rule refuses with a message that names the problem; ``reads`` is
held as a set in one order; what the definition ignores (prose, frontmatter, extra columns)
survives a structured save.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apis.shared.handoff.models import HANDOFF_REQUEST_MAX_CHARS
from apis.shared.handoff.runs import WORKFLOW_RUN_MAX_STEPS
from apis.shared.workflows.format import (
    CHECKLIST_REFUSED_FIELD_TYPE,
    MAX_REQUEST_CHARS,
    MAX_STEPS,
    Workflow,
    WorkflowError,
    WorkflowLayout,
    WorkflowResult,
    WorkflowStep,
    carry_layout,
    check_field_set_refs,
    checklist_field_set_names,
    parse_workflow,
    read_workflow_file,
    same_definition,
    serialize_workflow,
    slug_for_title,
    step_name_for,
    validate_workflow_upload,
    workflow_fingerprint,
    workflow_name_for,
)

FULL = """---
title: Proposal review
version: 3
files: one
result: merge
owner-note: kept as written
---

Read by the review office.

| step | kind | worker | reads | note |
|---|---|---|---|---|
| generalist | agent | AST-1 @ 5 | files, request | first |
| colleague | agent | AST-2 | request | |
| summary | agent | AST-3 @ 2 | colleague, generalist | last |

Prose after the table.

## Request

Review this proposal
against the solicitation.

## Step: summary

Merge the two reviews.

| keep | this table |
|---|---|
| 1 | 2 |

## Result

Reads: summary, generalist

Write one report.
"""


def _file(rows: str, *, front: str = "title: T\n") -> str:
    return f"---\n{front}---\n\n| step | kind | worker | reads |\n|---|---|---|---|\n{rows}"


def _refused(text: str, match: str) -> None:
    with pytest.raises(WorkflowError, match=match):
        parse_workflow(text, name="review")


class TestRoundTrip:
    def test_a_full_file_reads_as_its_parts(self):
        workflow, layout = read_workflow_file(FULL, name="proposal-review")

        assert workflow.title == "Proposal review"
        assert workflow.version == 3
        assert workflow.files == "one"
        assert workflow.request == "Review this proposal\nagainst the solicitation."
        assert [s.name for s in workflow.steps] == ["generalist", "colleague", "summary"]
        assert workflow.steps[0].agent_version == 5
        assert workflow.steps[1].agent_version is None
        # reads is a set, held in one order: inputs first, then steps in table order
        assert workflow.steps[0].reads == ("request", "files")
        assert workflow.steps[2].reads == ("generalist", "colleague")
        assert workflow.steps[2].request.startswith("Merge the two reviews.")
        assert "| keep | this table |" in workflow.steps[2].request
        assert workflow.result == WorkflowResult(
            mode="merge", reads=("generalist", "summary"), instruction="Write one report."
        )
        assert layout.preamble == "Read by the review office."
        assert layout.postscript == "Prose after the table."
        assert layout.frontmatter == ("owner-note: kept as written",)
        assert layout.unmanaged_columns == ("note",)
        assert layout.unmanaged_cells == (("first",), ("",), ("last",))

    def test_serializing_reads_back_the_same_and_is_byte_stable(self):
        workflow, layout = read_workflow_file(FULL, name="proposal-review")

        once = serialize_workflow(workflow, layout=layout)
        again = serialize_workflow(*read_workflow_file(once, name="proposal-review")[:1], layout=layout)

        assert read_workflow_file(once, name="proposal-review") == (workflow, layout)
        assert once == again

    def test_a_blank_workflow_is_a_title_and_an_empty_table(self):
        text = serialize_workflow(Workflow(name="blank", title="Untitled workflow"))

        assert text == (
            "---\ntitle: Untitled workflow\nversion: 1\n---\n\n"
            "| step | kind | worker | reads |\n|---|---|---|---|\n"
        )
        assert parse_workflow(text, name="blank") == Workflow(name="blank", title="Untitled workflow")

    def test_defaults_are_not_written(self):
        text = serialize_workflow(
            Workflow(name="x", title="T", steps=(WorkflowStep(name="a", agent_id="AST-1"),))
        )

        assert "files:" not in text and "result:" not in text and "## Result" not in text
        assert "| a | agent | AST-1 | request, files |" in text

    def test_reads_order_does_not_change_the_definition(self):
        a = Workflow(name="x", title="T", steps=(WorkflowStep(name="a", agent_id="AST-1", reads=("files", "request")),))
        b = replace(a, steps=(WorkflowStep(name="a", agent_id="AST-1", reads=("request", "files")),))

        assert serialize_workflow(a) == serialize_workflow(b)
        assert workflow_fingerprint(a) == workflow_fingerprint(b)

    def test_a_value_the_table_cannot_carry_is_refused_by_name(self):
        workflow = Workflow(
            name="x",
            title="T",
            request="## Result\nsneaky",
            steps=(WorkflowStep(name="a", agent_id="AST-1"),),
        )
        with pytest.raises(WorkflowError, match="the request cannot contain a line that starts a section"):
            serialize_workflow(workflow)


class TestEnvelope:
    def test_more_than_eight_steps_is_refused(self):
        rows = "".join(f"| s{i} | agent | AST-{i} | request |\n" for i in range(MAX_STEPS + 1))
        _refused(_file(rows), r"has 9 steps; a workflow holds at most 8")

    def test_the_limit_is_the_run_engines(self):
        assert MAX_STEPS == WORKFLOW_RUN_MAX_STEPS
        assert MAX_REQUEST_CHARS == HANDOFF_REQUEST_MAX_CHARS

    def test_a_cycle_is_refused_naming_its_steps(self):
        _refused(
            _file("| a | agent | AST-1 | b |\n| b | agent | AST-2 | c |\n| c | agent | AST-3 | a |\n"),
            r"steps read each other in a circle \(a → b → c → a\)",
        )

    def test_reading_a_step_that_does_not_exist_is_refused(self):
        _refused(_file("| a | agent | AST-1 | request, missing |\n"), r"reads 'missing', which is not a step")

    def test_reading_itself_is_refused(self):
        _refused(_file("| a | agent | AST-1 | a |\n"), "reads its own result")

    def test_a_step_named_twice_is_refused(self):
        _refused(_file("| a | agent | AST-1 | |\n| a | agent | AST-2 | |\n"), "two steps are named 'a'")

    def test_an_unknown_kind_is_refused(self):
        _refused(_file("| a | robot | AST-1 | |\n"), "unknown kind 'robot'")

    @pytest.mark.parametrize("name", ["request", "files"])
    def test_a_step_cannot_take_an_input_name(self, name):
        _refused(_file(f"| {name} | agent | AST-1 | |\n"), f"cannot be named '{name}'")

    def test_a_bad_step_name_is_refused(self):
        _refused(_file("| Bad-Name | agent | AST-1 | |\n"), "must be snake_case")

    def test_a_bad_agent_version_is_refused(self):
        _refused(_file("| a | agent | AST-1 @ two | |\n"), "Agent version must be a positive whole number")

    def test_the_result_must_read_real_steps(self):
        text = _file("| a | agent | AST-1 | |\n") + "\n## Result\n\nReads: a, ghost\n"
        _refused(text, "the result reads 'ghost'")

    def test_a_section_for_a_missing_step_is_refused(self):
        _refused(_file("| a | agent | AST-1 | |\n") + "\n## Step: ghost\n\nhi\n", "names no step in the table")

    def test_the_request_is_a_section_not_frontmatter(self):
        _refused(_file("", front="title: T\nrequest: do it\n"), "the request is a '## Request' section")

    def test_a_title_is_required(self):
        _refused(_file("", front="version: 1\n"), "needs a 'title:' line")

    def test_files_must_be_a_known_mode(self):
        _refused(_file("", front="title: T\nfiles: many\n"), "files must be one of none, one, any")

    def test_no_table_is_refused(self):
        _refused("---\ntitle: T\n---\n\nJust prose.\n", "no step table found")


class TestUpload:
    def test_only_workflow_files_are_parsed(self):
        assert validate_workflow_upload("forms.md", b"anything") is None
        assert validate_workflow_upload("field-set.budget.md", b"anything") is None

    def test_a_valid_file_parses(self):
        assert validate_workflow_upload("workflow.review.md", _file("| a | agent | AST-1 | |\n").encode()).title == "T"

    def test_a_name_that_would_never_list_is_refused(self):
        with pytest.raises(WorkflowError, match="lowercase words joined by hyphens"):
            validate_workflow_upload("workflow.Review.md", _file("").encode())

    def test_not_utf8_is_refused(self):
        with pytest.raises(WorkflowError, match="not UTF-8"):
            validate_workflow_upload("workflow.review.md", b"\xff\xfe")

    def test_names(self):
        assert workflow_name_for("references/workflow.proposal-review.md") == "proposal-review"
        assert workflow_name_for("workflow.md") is None


class TestHelpers:
    def test_same_definition_ignores_version_and_whitespace(self):
        a = Workflow(name="x", title="T", request="go", steps=(WorkflowStep(name="a", agent_id="AST-1"),))
        assert same_definition(a, replace(a, version=7, request="  go  "))
        assert not same_definition(a, replace(a, request="stop"))

    def test_carry_layout_follows_steps_by_name(self):
        before = Workflow(
            name="x", title="T", steps=(WorkflowStep(name="a", agent_id="AST-1"), WorkflowStep(name="b", agent_id="AST-2"))
        )
        layout = WorkflowLayout(unmanaged_columns=("note",), unmanaged_cells=(("for a",), ("for b",)))
        after = replace(before, steps=(before.steps[1], WorkflowStep(name="c", agent_id="AST-3")))

        carried = carry_layout(layout, before, after)

        assert carried.unmanaged_cells == (("for b",), ("",))
        assert "| b | agent | AST-2 | request, files | for b |" in serialize_workflow(after, layout=carried)

    def test_names_from_labels(self):
        assert slug_for_title("Proposal Review!", taken=["proposal-review"]) == "proposal-review-2"
        assert step_name_for("Budget Reader", taken=["budget_reader"]) == "budget_reader_2"
        assert step_name_for("42") == "step_42"
        assert step_name_for("Files") == "files_step"


HAIKU = "us.anthropic.claude-haiku-5-5-v1:0"
SONNET = "us.anthropic.claude-sonnet-5-5-v1:0"

PROMPT_FILE = f"""---
title: Panel
version: 2
---

| step | kind | worker | reads |
|---|---|---|---|
| generalist | agent | AST-1 @ 5 | request, files |
| fiscal | prompt | {HAIKU} | files |
| summary | prompt | {SONNET} | files, generalist, fiscal |

## Step: fiscal

Check the budget against the solicitation.

## Step: summary

Merge the two reviews.
"""


class TestPromptSteps:
    """A prompt step (workflow-designer §21.3): kind ``prompt``, a model id as its worker."""

    def test_a_prompt_step_reads_as_its_model_and_its_instruction(self):
        workflow = parse_workflow(PROMPT_FILE, name="panel")

        fiscal = workflow.step("fiscal")
        assert fiscal.kind == "prompt"
        assert fiscal.is_prompt
        assert fiscal.model_id == HAIKU
        assert fiscal.agent_id == ""
        assert fiscal.agent_version is None
        assert fiscal.worker == HAIKU
        assert fiscal.request == "Check the budget against the solicitation."
        assert workflow.step("summary").reads == ("files", "generalist", "fiscal")
        assert workflow.step("generalist").model_id is None

    def test_it_round_trips_byte_for_byte(self):
        workflow, layout = read_workflow_file(PROMPT_FILE, name="panel")

        text = serialize_workflow(workflow, layout=layout)

        assert text == PROMPT_FILE
        assert read_workflow_file(text, name="panel") == (workflow, layout)

    def test_the_fingerprint_names_the_model(self):
        workflow = parse_workflow(PROMPT_FILE, name="panel")
        other = parse_workflow(PROMPT_FILE.replace(f"| fiscal | prompt | {HAIKU}", f"| fiscal | prompt | {SONNET}"), name="panel")

        assert workflow_fingerprint(workflow) != workflow_fingerprint(other)

    def test_an_agent_only_file_keeps_its_fingerprint(self):
        """Pinned from ``develop`` before prompt steps: a run's recorded fingerprint must not move."""
        workflow = parse_workflow(FULL, name="proposal-review")

        assert workflow_fingerprint(workflow) == "2eba911c9973a3c9"

    def test_the_parser_accepts_it_whatever_the_flag(self, monkeypatch):
        monkeypatch.delenv("PROMPT_STEPS_ENABLED", raising=False)

        assert validate_workflow_upload("workflow.panel.md", PROMPT_FILE.encode()) is not None

    @pytest.mark.parametrize("worker", [f"{HAIKU} @ 2", "", "has space", "-leading-dash"])
    def test_a_worker_that_is_not_a_model_id_is_refused(self, worker):
        _refused(_file(f"| a | prompt | {worker} | files |\n"), "a prompt step's worker must be a model id")

    def test_an_arn_is_a_model_id(self):
        arn = "arn:aws:bedrock:us-west-2:000000000000:inference-profile/us.anthropic.claude-haiku-5-5-v1:0"
        assert parse_workflow(_file(f"| a | prompt | {arn} | files |\n"), name="review").steps[0].model_id == arn

    def test_a_definition_must_not_mix_an_agent_and_a_model(self):
        both = WorkflowStep(name="a", agent_id="AST-1", kind="prompt", model_id=HAIKU)
        with pytest.raises(WorkflowError, match="names no Agent"):
            serialize_workflow(Workflow(name="x", title="T", steps=(both,)))
        agent_with_model = WorkflowStep(name="a", agent_id="AST-1", model_id=HAIKU)
        with pytest.raises(WorkflowError, match="names no model"):
            serialize_workflow(Workflow(name="x", title="T", steps=(agent_with_model,)))
        no_model = WorkflowStep(name="a", agent_id="", kind="prompt")
        with pytest.raises(WorkflowError, match="is not a model id"):
            serialize_workflow(Workflow(name="x", title="T", steps=(no_model,)))

    def test_a_no_op_save_of_a_prompt_step_is_the_same_definition(self):
        workflow = parse_workflow(PROMPT_FILE, name="panel")

        assert same_definition(workflow, replace(workflow, version=9))


CHECKLIST_FILE = f"""---
title: Audit
version: 3
---

| step | kind | worker | reads |
|---|---|---|---|
| fiscal | prompt | {HAIKU} | files |
| audit | checklist | {SONNET} | files, fiscal |
| quick | checklist | {HAIKU} | request, files |

## Step: fiscal

Check the budget against the solicitation.

## Step: audit

Field sets: budget-caps, compliance

Treat a missing page as cant_tell.

## Step: quick

Field sets: compliance
"""


def _field_set(name: str, *types: str):
    """A real parsed field set, so the duck-typed check reads what the parser makes."""
    from apis.shared.extraction.field_sets import parse_field_set

    rows = "".join(f"| f{i} | F{i} | {t} | the value | {'a, b' if t == 'enum' else ''} | |\n" for i, t in enumerate(types))
    text = (
        f"---\ntitle: {name}\nversion: 1\n---\n\n| name | title | type | find | enum | optional |\n"
        f"|---|---|---|---|---|---|\n{rows}"
    )
    return parse_field_set(text, name=name)


def _checklist_file(section: str, reads: str = "files") -> str:
    return _file(f"| a | checklist | {HAIKU} | {reads} |\n") + f"\n## Step: a\n\n{section}"


class TestChecklistSteps:
    """A checklist step (workflow-designer §22.3): a model id, and a ``Field sets:`` line."""

    def test_a_checklist_step_reads_as_its_model_its_sets_and_its_guidance(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")

        audit = workflow.step("audit")
        assert audit.kind == "checklist"
        assert audit.is_checklist and audit.is_model_step and not audit.is_prompt
        assert audit.model_id == SONNET and audit.worker == SONNET
        assert audit.agent_id == "" and audit.agent_version is None
        assert audit.field_sets == ("budget-caps", "compliance")
        assert audit.request == "Treat a missing page as cant_tell."
        assert audit.reads == ("files", "fiscal")
        quick = workflow.step("quick")
        assert quick.field_sets == ("compliance",)
        assert quick.request is None
        assert workflow.step("fiscal").field_sets == ()

    def test_it_round_trips_byte_for_byte(self):
        workflow, layout = read_workflow_file(CHECKLIST_FILE, name="audit")

        text = serialize_workflow(workflow, layout=layout)

        assert text == CHECKLIST_FILE
        assert read_workflow_file(text, name="audit") == (workflow, layout)

    def test_a_definition_writes_the_field_sets_line_even_with_no_guidance(self):
        step = WorkflowStep(name="a", agent_id="", kind="checklist", model_id=HAIKU, reads=("files",), field_sets=("x",))
        text = serialize_workflow(Workflow(name="x", title="T", steps=(step,)))

        assert "## Step: a\n\nField sets: x\n" in text
        assert parse_workflow(text, name="x").steps[0] == step

    def test_a_no_op_save_is_the_same_definition(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")

        assert same_definition(workflow, replace(workflow, version=9))
        assert serialize_workflow(workflow) == serialize_workflow(parse_workflow(serialize_workflow(workflow), name="audit"))

    def test_the_sets_and_their_order_are_part_of_the_definition(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")
        swapped = parse_workflow(CHECKLIST_FILE.replace("budget-caps, compliance", "compliance, budget-caps"), name="audit")

        assert not same_definition(workflow, swapped)
        assert workflow_fingerprint(workflow) != workflow_fingerprint(swapped)

    def test_files_without_a_checklist_step_keep_their_fingerprints(self):
        assert workflow_fingerprint(parse_workflow(FULL, name="proposal-review")) == "2eba911c9973a3c9"

    def test_the_parser_accepts_it_whatever_the_flag(self, monkeypatch):
        monkeypatch.delenv("CHECKLIST_STEPS_ENABLED", raising=False)
        monkeypatch.delenv("PROMPT_STEPS_ENABLED", raising=False)

        assert validate_workflow_upload("workflow.audit.md", CHECKLIST_FILE.encode()) is not None

    def test_the_section_must_open_with_the_field_sets_line(self):
        _refused(_checklist_file("Be strict.\n\nField sets: x\n"), "opens with a 'Field sets:' line")

    def test_a_checklist_step_with_no_section_is_refused(self):
        _refused(_file(f"| a | checklist | {HAIKU} | files |\n"), "opens with a 'Field sets:' line")

    def test_an_empty_field_sets_line_is_refused(self):
        _refused(_checklist_file("Field sets:\n"), "names at least one field set")

    def test_a_set_named_twice_is_refused(self):
        _refused(_checklist_file("Field sets: x, y, x\n"), "names the field set 'x' twice")

    def test_more_than_sixteen_sets_is_refused(self):
        names = ", ".join(f"s{i}" for i in range(17))
        _refused(_checklist_file(f"Field sets: {names}\n"), "names 17 field sets; a checklist step runs at most 16")

    def test_sixteen_sets_parse(self):
        names = ", ".join(f"s{i}" for i in range(16))
        assert len(parse_workflow(_checklist_file(f"Field sets: {names}\n"), name="review").steps[0].field_sets) == 16

    def test_a_bad_set_name_is_refused(self):
        _refused(_checklist_file("Field sets: Budget_Caps\n"), "is not a field-set name")

    @pytest.mark.parametrize("reads", ["", "request", "request, b"])
    def test_a_checklist_step_must_read_files(self, reads):
        text = _file(f"| a | checklist | {HAIKU} | {reads} |\n| b | prompt | {HAIKU} | files |\n") + "\n## Step: a\n\nField sets: x\n"
        _refused(text, "must read 'files'")

    def test_a_worker_that_is_not_a_model_id_is_refused(self):
        _refused(_checklist_file("Field sets: x\n").replace(HAIKU, f"{HAIKU} @ 2"), "a checklist step's worker must be a model id")

    def test_only_a_checklist_step_names_field_sets(self):
        step = WorkflowStep(name="a", agent_id="", kind="prompt", model_id=HAIKU, field_sets=("x",))
        with pytest.raises(WorkflowError, match="only a checklist step runs field sets"):
            serialize_workflow(Workflow(name="x", title="T", steps=(step,)))

    def test_an_agent_steps_request_may_start_with_field_sets(self):
        text = _file("| a | agent | AST-1 | |\n") + "\n## Step: a\n\nField sets: are a thing to mention\n"
        assert parse_workflow(text, name="review").steps[0].request == "Field sets: are a thing to mention"


class TestChecklistFieldSetRefs:
    """``check_field_set_refs``: the sets must be the skill's own, with no ``list`` field (§22.3)."""

    def test_sets_the_skill_has_pass(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")
        sets = {"budget-caps": _field_set("budget-caps", "currency", "number"), "compliance": _field_set("compliance", "enum")}

        check_field_set_refs(workflow, sets)
        assert validate_workflow_upload("workflow.audit.md", CHECKLIST_FILE.encode(), field_sets=sets) is not None

    def test_a_set_missing_from_the_skill_is_refused(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")
        with pytest.raises(WorkflowError, match="step 'audit': the field set 'budget-caps' is not in this skill"):
            check_field_set_refs(workflow, {"compliance": _field_set("compliance", "enum")})

    def test_a_set_with_a_list_field_is_refused(self):
        workflow = parse_workflow(CHECKLIST_FILE, name="audit")
        sets = {"budget-caps": _field_set("budget-caps", "string", "list"), "compliance": _field_set("compliance", "enum")}
        with pytest.raises(WorkflowError, match="the field set 'budget-caps' has a list field, 'f1'"):
            check_field_set_refs(workflow, sets)

    def test_the_upload_check_refuses_with_the_skills_sets(self):
        with pytest.raises(WorkflowError, match="is not in this skill"):
            validate_workflow_upload("workflow.audit.md", CHECKLIST_FILE.encode(), field_sets={})

    def test_names_are_listed_once_in_first_named_order(self):
        assert checklist_field_set_names(parse_workflow(CHECKLIST_FILE, name="audit")) == ("budget-caps", "compliance")

    def test_the_constants_match_the_field_set_parsers(self):
        from apis.shared.extraction import field_sets as fs

        assert CHECKLIST_REFUSED_FIELD_TYPE == fs.TYPE_LIST
        for name in ("budget-caps", "a", "x9-y"):
            assert fs.field_set_name_for(f"field-set.{name}.md") == name
        for bad in ("Budget", "a_b", "-a", "a-"):
            assert fs.field_set_name_for(f"field-set.{bad}.md") is None
            with pytest.raises(WorkflowError):
                parse_workflow(_checklist_file(f"Field sets: {bad}\n"), name="review")
