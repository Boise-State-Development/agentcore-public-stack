"""Word tracked changes behind ``WORD_TRACKED_CHANGES_ENABLED`` (docs/specs/word-tracked-changes.md).

Off, every surface reads a ``.docx`` exactly as before and ``redline_word_document``
is never built. On, the three read surfaces see each change written out, and the
Word family gains the redline tool. Measured on dev before the change: an inline
redline reached Haiku as "State of IdahoDelaware".
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from apis.inference_api.chat.models import FileContent
from apis.inference_api.chat.routes import _build_word_document_tools, _write_out_tracked_changes
from apis.shared.feature_flags import word_tracked_changes_enabled
from apis.shared.files.document_read import docx_paragraphs
from tests.shared.docx_fixtures import build_docx, deleted, inserted, paragraph, run

REDLINE = build_docx(paragraph(run("Governed by the laws of "), deleted("Idaho"), inserted("Delaware"), run(".")))
CLEAN = build_docx(paragraph(run("Governed by the laws of Idaho.")))
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("WORD_TRACKED_CHANGES_ENABLED", raising=False)


@pytest.mark.parametrize("value,expected", [(None, False), ("", False), ("false", False), ("1", False), ("true", True), (" TRUE ", True)])
def test_the_flag_is_opt_in(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("WORD_TRACKED_CHANGES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", value)
    assert word_tracked_changes_enabled() is expected


class TestDocxParagraphs:
    """R1: ``document_read``, the upload digest and text-only models all read through here."""

    def test_off_a_deletion_vanishes_as_before(self, off):
        assert docx_paragraphs(REDLINE) == ["Governed by the laws of Delaware."]

    def test_on_both_sides_of_the_change_are_written_out(self, on):
        paragraphs = docx_paragraphs(REDLINE)
        assert paragraphs[0].startswith("Note: this document contains tracked changes by Sponsor Counsel.")
        assert paragraphs[1:] == ["Governed by the laws of [deleted: Idaho][inserted: Delaware]."]

    def test_on_a_document_without_changes_reads_as_before(self, on):
        assert docx_paragraphs(CLEAN) == ["Governed by the laws of Idaho."]


def _file(name: str, data: bytes, mime: str = DOCX_MIME) -> FileContent:
    return FileContent(filename=name, content_type=mime, bytes=base64.b64encode(data).decode("ascii"))


class TestInlineAttachments:
    """R3: a redlined ``.docx`` attachment reaches Bedrock already annotated."""

    @pytest.mark.asyncio
    async def test_off_the_list_is_returned_untouched(self, off):
        files = [_file("contract.docx", REDLINE)]
        assert await _write_out_tracked_changes(files) is files

    @pytest.mark.asyncio
    async def test_on_only_a_redlined_docx_is_rewritten(self, on):
        pdf = _file("brief.pdf", b"%PDF-1.4", "application/pdf")
        clean = _file("clean.docx", CLEAN)
        redlined = _file("contract.docx", REDLINE)

        out = await _write_out_tracked_changes([pdf, clean, redlined])

        assert out[0] is pdf and out[1] is clean
        assert out[2].filename == "contract.docx" and out[2].content_type == DOCX_MIME
        assert docx_paragraphs(base64.b64decode(out[2].bytes))[1] == (
            "Governed by the laws of [deleted: Idaho][inserted: Delaware]."
        )

    @pytest.mark.asyncio
    async def test_on_a_file_that_is_not_a_word_package_is_sent_as_it_was(self, on):
        broken = _file("contract.docx", b"not a zip")
        assert (await _write_out_tracked_changes([broken]))[0] is broken

    @pytest.mark.asyncio
    async def test_on_a_rewrite_that_raises_keeps_the_original(self, on, monkeypatch):
        import apis.inference_api.chat.routes as routes

        def boom(file):
            raise RuntimeError("boom")

        monkeypatch.setattr(routes, "_annotated_inline_docx", boom)
        redlined = _file("contract.docx", REDLINE)
        assert (await _write_out_tracked_changes([redlined]))[0] is redlined


def _names(enabled_tools):
    return [t.tool_name for t in _build_word_document_tools(enabled_tools, "s1", "u1", "Pat Reviewer")]


WORD_TOOLS = ["create_word_document", "modify_word_document", "list_word_documents", "read_word_document"]


class TestInjection:
    def test_off_the_family_is_unchanged(self, off):
        assert _names(["create_word_document"]) == WORD_TOOLS

    def test_on_the_family_gains_the_redline_tool(self, on):
        assert _names(["create_word_document"]) == WORD_TOOLS + ["redline_word_document"]

    def test_on_it_still_needs_the_word_grant(self, on):
        assert _names(["web_search"]) == []


def _document_xml(docx: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(docx)) as package:
        return package.read("word/document.xml")


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


@pytest.fixture()
def store(monkeypatch):
    """A Word document in the chat, and a capture of what gets saved."""
    import agents.builtin_tools.word_document_tool as word

    source = SimpleNamespace(filename="services-agreement.docx", s3_bucket="b", s3_key="k")
    saved = {}

    async def find(user_id, session_id, name):
        return source if name.removesuffix(".docx") == "services-agreement" else None

    async def save(user_id, session_id, filename, data, mime):
        saved.update(filename=filename, data=data, mime=mime)
        return "up-1", "4.2 KB"

    monkeypatch.setattr(word, "_storage_configured", lambda: True)
    monkeypatch.setattr(word, "_find_word_document", find)
    monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: CLEAN)
    monkeypatch.setattr(word, "_store_document", save)
    return saved


class TestRedlineTool:
    @pytest.mark.asyncio
    async def test_saves_a_new_file_and_reports_each_change(self, store):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        tool = make_redline_word_document_tool("s1", "u1", "Pat Reviewer")
        result = await _call(
            tool,
            document_name="services-agreement",
            changes=[{"find": "laws of Idaho", "replace": "laws of Delaware", "comment": "Per counsel."}],
        )

        card = json.loads(result)
        assert card["payload"] == {"filename": "services-agreement-redline.docx", "upload_id": "up-1", "size_kb": "4.2 KB"}
        assert 'Tracked changes by Pat Reviewer: 1. deleted "Idaho", inserted "Delaware", with a comment' in card["summary"]
        assert store["filename"] == "services-agreement-redline.docx"
        assert store["mime"] == DOCX_MIME
        assert docx_paragraphs(store["data"]) == ["Governed by the laws of Delaware."]

    @pytest.mark.asyncio
    async def test_author_and_output_name_can_be_given(self, store):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        tool = make_redline_word_document_tool("s1", "u1", "Pat Reviewer")
        result = await _call(
            tool,
            document_name="services-agreement.docx",
            changes=[{"find": "Idaho", "replace": "Utah"}],
            author="Outside Counsel",
            output_name="agreement-v2.docx",
        )

        assert "Tracked changes by Outside Counsel" in json.loads(result)["summary"]
        assert store["filename"] == "agreement-v2.docx"
        assert b'w:author="Outside Counsel"' in _document_xml(store["data"])

    @pytest.mark.asyncio
    async def test_a_change_that_cannot_be_placed_saves_nothing(self, store):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        tool = make_redline_word_document_tool("s1", "u1", "Pat Reviewer")
        result = await _call(tool, document_name="services-agreement", changes=[{"find": "Ohio", "replace": "Utah"}])

        assert result["status"] == "error"
        assert 'Change 1: "Ohio" was not found.' in result["content"][0]["text"]
        assert store == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes,problem",
        [
            ([], "`changes` must be a non-empty list"),
            (["Idaho"], "Change 1: expected an object with `find`."),
            ([{"find": 3, "replace": "x"}], "Change 1: `find` must be text."),
            ([{"find": "Idaho", "occurrence": "2", "comment": "c"}], "Change 1: `occurrence` must be a whole number."),
        ],
    )
    async def test_malformed_changes_are_reported(self, store, changes, problem):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        tool = make_redline_word_document_tool("s1", "u1", None)
        result = await _call(tool, document_name="services-agreement", changes=changes)

        assert result["status"] == "error"
        assert problem in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_an_unknown_document_is_named(self, store):
        from agents.builtin_tools.word_document_tool import make_redline_word_document_tool

        tool = make_redline_word_document_tool("s1", "u1", None)
        result = await _call(tool, document_name="nda", changes=[{"find": "a", "replace": "b"}])

        assert "No Word document named 'nda'" in result["content"][0]["text"]


class TestReadWordDocument:
    """R2: python-docx in the sandbox sees the markers as ordinary runs."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag,expected", [("true", True), ("", False)])
    async def test_the_sandbox_gets_the_annotated_bytes_only_with_the_flag(self, store, monkeypatch, flag, expected):
        import agents.builtin_tools.word_document_tool as word

        monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", flag)
        monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: REDLINE)
        monkeypatch.setattr(word, "_get_code_interpreter_id", lambda: "ci-1")
        sent = []
        monkeypatch.setattr(word, "_extract_docx_text", lambda ci, data: sent.append(data) or "text")

        tool = word.make_read_word_document_tool("s1", "u1")
        await _call(tool, document_name="services-agreement")

        assert (b"[deleted: " in _document_xml(sent[0])) is expected
