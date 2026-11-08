"""``redline_word_document``'s ``if_unplaced`` argument (docs/specs/word-tracked-changes.md §3b).

``"fail"``, the default, is the all-or-nothing call as before. ``"comment"``
saves the placeable changes and reports the rest as ACTION NEEDED comments, with
applied and commented changes listed apart. The spec that documents it is pinned
in ``test_word_redline_paragraphs.SPEC_DIGEST``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apis.inference_api.chat.routes import _build_word_document_tools
from apis.shared.files.document_read import docx_paragraphs
from tests.apis.inference_api.test_word_redline_paragraphs import SPEC_DIGEST, _digest
from tests.shared.docx_fixtures import build_docx, paragraph, run

CONTRACT = build_docx(
    paragraph(run("SERVICES AGREEMENT", bold=True)),
    paragraph(run("1. Payment. Invoices are payable within thirty (30) days of receipt.")),
    paragraph(run("2. Term. The term is one year, renewable by the parties.")),
)


def _said(result) -> str:
    if isinstance(result, dict):
        return "\n".join(block.get("text", "") for block in result.get("content", []))
    return json.loads(result)["summary"]


def _redline_tool(monkeypatch):
    monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")
    tools = _build_word_document_tools(["create_word_document"], "s1", "u1", "Pat Reviewer")
    return next(t for t in tools if t.tool_name == "redline_word_document")


class TestSpec:
    def test_if_unplaced_is_optional_and_defaults_to_fail(self, monkeypatch):
        tool = _redline_tool(monkeypatch)
        assert _digest(tool) == SPEC_DIGEST
        schema = tool.tool_spec["inputSchema"]["json"]
        assert schema["properties"]["if_unplaced"]["default"] == "fail"
        assert "if_unplaced" not in schema["required"]


@pytest.fixture()
def store(monkeypatch):
    import agents.builtin_tools.word_document_tool as word

    source = SimpleNamespace(filename="services-agreement.docx", s3_bucket="b", s3_key="k")
    saved = {}

    async def find(user_id, session_id, name):
        return source

    async def save(user_id, session_id, filename, data, mime):
        saved.update(filename=filename, data=data)
        return "up-1", "6.1 KB"

    monkeypatch.setattr(word, "_storage_configured", lambda: True)
    monkeypatch.setattr(word, "_find_word_document", find)
    monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: CONTRACT)
    monkeypatch.setattr(word, "_store_document", save)
    return saved


async def _call(tool, **kwargs):
    return await tool._tool_func(**kwargs)


MIXED = [
    {"find": "within thirty (30) days", "replace": "within sixty (60) days"},
    {"find": "force majeure", "comment": "Add a force majeure clause."},
    {"find": "one year", "replace": "two years"},
]


class TestCall:
    @pytest.mark.asyncio
    async def test_comment_mode_saves_and_lists_applied_and_commented_apart(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        said = _said(await _call(tool, document_name="services-agreement", changes=MIXED, if_unplaced="comment"))
        assert (
            'Tracked changes by Pat Reviewer: 1. deleted "thirty (30)", inserted "sixty (60)"; '
            '3. deleted "one year", inserted "two years". '
            'Not placed, left as ACTION NEEDED comments (1): '
            '2. "force majeure": The wording to change was not found in the document.'
        ) in said
        paragraphs = docx_paragraphs(store["data"])
        assert any(p.startswith("SERVICES AGREEMENT [comment by Pat Reviewer: ACTION NEEDED —") for p in paragraphs)

    @pytest.mark.asyncio
    async def test_when_nothing_can_be_placed_it_says_so(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        said = _said(await _call(tool, document_name="services-agreement", changes=[MIXED[1]], if_unplaced="comment"))
        assert "No change could be placed. Not placed, left as ACTION NEEDED comments (1): 1." in said
        assert store["filename"] == "services-agreement-redline.docx"

    @pytest.mark.asyncio
    async def test_the_default_is_still_all_or_nothing(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        said = _said(await _call(tool, document_name="services-agreement", changes=MIXED))
        assert said.startswith("❌ Nothing was changed in 'services-agreement.docx':")
        assert '- Change 2: "force majeure" was not found.' in said
        assert store == {}

    @pytest.mark.asyncio
    async def test_with_nothing_unplaced_the_summary_reads_as_before(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        changes = [MIXED[0], MIXED[2]]
        said = _said(await _call(tool, document_name="services-agreement", changes=changes, if_unplaced="comment"))
        assert 'Tracked changes by Pat Reviewer: 1. deleted "thirty (30)", inserted "sixty (60)"; 2. deleted "one year", inserted "two years". ' in said
        assert "ACTION NEEDED" not in said

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["skip", "", 3, None])
    async def test_an_unknown_mode_changes_nothing(self, monkeypatch, store, value):
        tool = _redline_tool(monkeypatch)
        said = _said(await _call(tool, document_name="services-agreement", changes=MIXED, if_unplaced=value))
        assert '`if_unplaced` must be "fail" or "comment".' in said
        assert store == {}

    @pytest.mark.asyncio
    async def test_a_callers_problem_still_fails_in_comment_mode(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        changes = MIXED + [{"find": "Payment"}]
        said = _said(await _call(tool, document_name="services-agreement", changes=changes, if_unplaced="comment"))
        assert said.startswith("❌ Nothing was changed in 'services-agreement.docx':")
        assert "Change 4: give `replace`" in said and "force majeure" not in said
        assert store == {}
