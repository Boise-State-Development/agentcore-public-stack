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
    MAX_REQUEST_CHARS,
    MAX_STEPS,
    Workflow,
    WorkflowError,
    WorkflowLayout,
    WorkflowResult,
    WorkflowStep,
    carry_layout,
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

    @pytest.mark.parametrize("kind", ["prompt", "checklist"])
    def test_reserved_kinds_are_refused_with_a_sentence(self, kind):
        _refused(_file(f"| a | {kind} | m | |\n"), f"'{kind}' steps are not available yet")

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
