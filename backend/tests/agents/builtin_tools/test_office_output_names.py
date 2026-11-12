"""Output names for the Office edit tools are made safe, never refused.

A refused name makes the model re-send the whole call to fix a file name: a
redline's every change, an edit's whole program. Dev, 2026-11-11: a 13-change
``redline_word_document`` call failed on ``output_name`` "... - redline"
because of the spaces. And with no output name at all, an edit of an uploaded
"Q3 Budget (draft).xlsx" failed its own default name.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.builtin_tools.excel_spreadsheet_tool as excel
import agents.builtin_tools.powerpoint_presentation_tool as pptx
import agents.builtin_tools.word_document_tool as word
from agents.builtin_tools.office._storage import (
    _output_document_name,
    _safe_document_name,
    _validate_document_name,
)


class TestSafeDocumentName:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Example Studies Workload and Evaluation Procedures - redline",
             "Example-Studies-Workload-and-Evaluation-Procedures-redline"),
            ("Q3 Budget (draft)", "Q3-Budget-draft"),
            ("Résumé notes", "Resume-notes"),
            ("already-safe_name", "already-safe_name"),
            ("a--b", "a-b"),
            ("../x", "x"),
            ("規約", ""),
        ],
    )
    def test_reduces_to_what_the_validator_accepts(self, name, expected):
        safe = _safe_document_name(name)
        assert safe == expected
        if safe:
            assert _validate_document_name(safe) == (True, None)


class TestOutputDocumentName:
    def test_a_given_name_wins_and_loses_its_extension(self):
        assert _output_document_name("Final Copy.DOCX", "source", ".docx") == "Final-Copy"

    def test_no_given_name_uses_the_default(self):
        assert _output_document_name(None, "Q3 Budget (draft).xlsx", ".xlsx") == "Q3-Budget-draft"

    def test_a_given_name_with_nothing_usable_falls_back_to_the_default(self):
        assert _output_document_name("???", "source-redline", ".docx") == "source-redline"

    def test_nothing_usable_anywhere_is_document(self):
        assert _output_document_name("", "規約.pptx", ".pptx") == "document"


def _source(filename: str) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, s3_bucket="b", s3_key="k")


def _stub_module(monkeypatch, module, find_name: str, modify_name: str, source_name: str) -> dict:
    """Stub a tool module's sandbox and store; return what gets saved."""
    saved: dict = {}

    async def find(user_id, session_id, name):
        return _source(source_name)

    async def store(user_id, session_id, filename, file_bytes, mime):
        saved["filename"] = filename
        return "up-1", "1.0 KB"

    def modify(code_interpreter_id, source_bytes, python_code, output_filename):
        saved["sandbox_filename"] = output_filename
        return b"edited"

    monkeypatch.setattr(module, "_get_code_interpreter_id", lambda: "ci-test")
    monkeypatch.setattr(module, "_storage_configured", lambda: True)
    monkeypatch.setattr(module, find_name, find)
    monkeypatch.setattr(module, "_download_s3_bytes", lambda bucket, key: b"source")
    monkeypatch.setattr(module, modify_name, modify)
    monkeypatch.setattr(module, "_store_document", store)
    return saved


class TestModifyTools:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("output_name", [None, "Q3 Budget (draft)"])
    async def test_word(self, monkeypatch, output_name):
        saved = _stub_module(monkeypatch, word, "_find_word_document", "_modify_docx_bytes", "Q3 Budget (draft).docx")
        tool = word.make_modify_word_document_tool("s1", "u1")
        result = await tool._tool_func(
            document_name="Q3 Budget (draft)", python_code="pass", output_name=output_name
        )
        assert json.loads(result)["payload"]["filename"] == "Q3-Budget-draft.docx"
        assert saved == {"sandbox_filename": "Q3-Budget-draft.docx", "filename": "Q3-Budget-draft.docx"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("output_name", [None, "Q3 Budget (draft).xlsx"])
    async def test_excel(self, monkeypatch, output_name):
        saved = _stub_module(monkeypatch, excel, "_find_excel_spreadsheet", "_modify_xlsx_bytes", "Q3 Budget (draft).xlsx")
        tool = excel.make_modify_excel_spreadsheet_tool("s1", "u1")
        result = await tool._tool_func(
            spreadsheet_name="Q3 Budget (draft)", python_code="pass", output_name=output_name
        )
        assert json.loads(result)["payload"]["filename"] == "Q3-Budget-draft.xlsx"
        assert saved == {"sandbox_filename": "Q3-Budget-draft.xlsx", "filename": "Q3-Budget-draft.xlsx"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("output_name", [None, "Q3 Budget (draft)"])
    async def test_powerpoint(self, monkeypatch, output_name):
        saved = _stub_module(
            monkeypatch, pptx, "_find_powerpoint_presentation", "_modify_pptx_bytes", "Q3 Budget (draft).pptx"
        )
        tool = pptx.make_modify_powerpoint_presentation_tool("s1", "u1")
        result = await tool._tool_func(
            presentation_name="Q3 Budget (draft)", python_code="pass", output_name=output_name
        )
        assert json.loads(result)["payload"]["filename"] == "Q3-Budget-draft.pptx"
        assert saved == {"sandbox_filename": "Q3-Budget-draft.pptx", "filename": "Q3-Budget-draft.pptx"}
