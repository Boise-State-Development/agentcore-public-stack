"""``redline_word_document`` inserting whole paragraphs, behind ``WORD_REDLINE_PARAGRAPHS_ENABLED``.

Off, the tool's spec is byte-identical to before the feature and an ``insert``
key is ignored as any unknown key is. On, the spec gains the ``insert`` /
``paragraphs`` wording once per deployment (pinned below) and changes can add
paragraphs (docs/specs/word-tracked-changes.md §3a).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apis.inference_api.chat.routes import _build_word_document_tools
from apis.shared.feature_flags import word_redline_paragraphs_enabled
from apis.shared.files.document_read import docx_paragraphs

FIXTURE = (Path(__file__).parents[2] / "agents" / "builtin_tools" / "fixtures" / "contract-numbered.docx").read_bytes()

#: The spec before this feature existed. Off, it must not move by a byte.
OFF_DIGEST = "d143bc01b17e7e914991f25e7f835b64eb5f4084313002a61544f96de7a50f27"
#: On: one toolConfig rewrite per deployment when the flag turns on (about 250
#: more cl100k tokens: 515 → 767). A change here is a deliberate spec change.
ON_DIGEST = "8907f51944826aaef446fffd25c90bd00c5dbe08de9c004d953e21077543c864"


def _digest(tool) -> str:
    return hashlib.sha256(json.dumps(tool.tool_spec, sort_keys=True).encode()).hexdigest()


def _changes_doc(tool) -> str:
    return tool.tool_spec["inputSchema"]["json"]["properties"]["changes"]["description"]


def _said(result) -> str:
    """The text a tool call returned, error or download card alike."""
    if isinstance(result, dict):
        return "\n".join(block.get("text", "") for block in result.get("content", []))
    return json.loads(result)["summary"]


def _redline_tool(monkeypatch, flag: str):
    monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")
    monkeypatch.setenv("WORD_REDLINE_PARAGRAPHS_ENABLED", flag)
    tools = _build_word_document_tools(["create_word_document"], "s1", "u1", "Pat Reviewer")
    return next(t for t in tools if t.tool_name == "redline_word_document")


@pytest.mark.parametrize(
    "value,expected", [(None, False), ("", False), ("1", False), ("yes", False), ("true", True), (" TRUE ", True)]
)
def test_the_flag_is_off_unless_true(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("WORD_REDLINE_PARAGRAPHS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WORD_REDLINE_PARAGRAPHS_ENABLED", value)
    assert word_redline_paragraphs_enabled() is expected


class TestSpec:
    def test_off_the_spec_is_unchanged(self, monkeypatch):
        tool = _redline_tool(monkeypatch, "")
        assert _digest(tool) == OFF_DIGEST
        assert "paragraphs" not in _changes_doc(tool)

    def test_on_the_spec_is_pinned(self, monkeypatch):
        tool = _redline_tool(monkeypatch, "true")
        assert _digest(tool) == ON_DIGEST
        assert '- insert: "after" or "before"' in _changes_doc(tool)

    def test_on_the_spec_does_not_depend_on_identity(self, monkeypatch):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        a = make_redline_word_document_tool("s1", "u1", "Pat Reviewer", insert_paragraphs=True)
        b = make_redline_word_document_tool("s2", "u2", "Lee Counsel", insert_paragraphs=True)
        assert _digest(a) == _digest(b) == ON_DIGEST


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
    monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: FIXTURE)
    monkeypatch.setattr(word, "_store_document", save)
    return saved


async def _call(tool, **kwargs):
    return await tool._tool_func(**kwargs)


NEW_SECTION = {
    "find": "indirect, incidental or consequential damages",
    "insert": "after",
    "paragraphs": [
        {"text": "Survival", "like": "Limitation of Liability"},
        "Sections 3 and 4 survive termination of this Agreement.",
    ],
    "comment": "New section.",
}


class TestCall:
    @pytest.mark.asyncio
    async def test_on_a_change_adds_paragraphs(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch, "true")
        said = _said(await _call(tool, document_name="services-agreement", changes=[NEW_SECTION]))
        assert "Tracked changes by Pat Reviewer: 1. inserted 2 paragraphs after" in said
        assert "with a comment on that paragraph" in said
        # What the model reads back (``document_read``, the digest): each new
        # paragraph is a tracked insertion, and the comment is on the anchor.
        paragraphs = docx_paragraphs(store["data"])
        at = paragraphs.index(
            "Neither party shall be liable for indirect, incidental or consequential damages. "
            "[comment by Pat Reviewer: New section.]"
        )
        assert paragraphs[at + 1 : at + 4] == [
            "[inserted: Survival]",
            "[inserted: Sections 3 and 4 survive termination of this Agreement.]",
            "Entire Agreement",
        ]

    @pytest.mark.asyncio
    async def test_off_an_insert_key_is_ignored_as_before(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch, "false")
        change = {k: v for k, v in NEW_SECTION.items() if k != "comment"}
        result = await _call(tool, document_name="services-agreement", changes=[change])
        assert "give `replace`" in _said(result)
        assert store == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change,problem",
        [
            ({"find": "Kickoff", "insert": "after", "paragraphs": "one line"}, "`paragraphs` must be a non-empty list"),
            ({"find": "Kickoff", "insert": "after", "paragraphs": []}, "`paragraphs` must be a non-empty list"),
            ({"find": "Kickoff", "insert": "after", "paragraphs": [7]}, "new paragraph 1 must be an object"),
            ({"find": "Kickoff", "insert": "after", "paragraphs": [{"like": "x"}]}, "new paragraph 1 needs `text`"),
            ({"find": "Kickoff", "insert": "after", "paragraphs": [{"text": "x", "lead": 3}]}, "`lead` must be text"),
            ({"find": "Kickoff", "paragraphs": ["x"]}, '`insert` must be "after" or "before"'),
            ({"find": "Kickoff", "insert": 1, "paragraphs": ["x"]}, '`insert` must be "after" or "before"'),
        ],
    )
    async def test_malformed_insertions_change_nothing(self, monkeypatch, store, change, problem):
        tool = _redline_tool(monkeypatch, "true")
        result = await _call(tool, document_name="services-agreement", changes=[change])
        assert problem in _said(result)
        assert store == {}

    @pytest.mark.asyncio
    async def test_engine_problems_come_back_one_per_line(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch, "true")
        change = {"find": "Kickoff", "insert": "after", "paragraphs": [{"text": "x", "like": "Limitation of Liability"}]}
        result = _said(await _call(tool, document_name="services-agreement", changes=[change]))
        assert "Drop `like` on the last new paragraph" in result
        assert store == {}
