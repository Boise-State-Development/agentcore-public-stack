"""``project_files`` (agents/builtin_tools/project_files_tool.py).

The tool is closed over one project's harness and the invoking member. The
listing, the text extraction and the member lookup are patched at the module
seam; the moto-backed end-to-end read (tracked changes included) is in
``tests/shared/test_kb_documents.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from agents.builtin_tools import project_files_tool as pf
from agents.builtin_tools.project_files_tool import (
    NO_ACCESS,
    PROJECT_FILES_LIST_MAX,
    ProjectFilesError,
    make_project_files_tool,
    match_project_file,
)
from apis.shared.assistants.kb_access import granted
from apis.shared.auth.models import User
from apis.shared.files.workspace import WORKSPACE_READ_MAX_BYTES

MODULE = "agents.builtin_tools.project_files_tool"
PROJECT = "prj_bio101"
HARNESS = "ast_harness_1"
MEMBER = User(user_id="u-member", email="member@example.edu", name="Mem", roles=["default"])
OTHER = User(user_id="u-other", email="other@example.edu", name="Oth", roles=["default"])
ACCESS = granted(HARNESS, MEMBER.user_id, "viewer")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The spec's digest. It is part of every project conversation's cached
#: ``toolConfig`` prefix, so a change here re-writes that prefix for every
#: project on deploy: change it on purpose, and say so in the PR.
PINNED_SPEC = "fdb46330ac9c8360e4fce4342dc0bd01e3a8826aad9bc951980416433eff05c9"


def _doc(doc_id: str, filename: str, *, status: str = "complete", size: int = 1200, content_type: str = DOCX,
         added_by: str = "u-owner", created: str = "2026-10-01T00:00:00Z") -> Dict[str, Any]:
    return {
        "documentId": doc_id, "assistantId": HARNESS, "filename": filename, "contentType": content_type,
        "sizeBytes": size, "s3Key": f"assistants/{HARNESS}/documents/{doc_id}/{filename}", "status": status,
        "createdAt": created, "updatedAt": created, "addedByUserId": added_by,
    }


class _Files:
    """The patched seams, in one place."""

    def __init__(self, monkeypatch) -> None:
        self.docs: List[Dict[str, Any]] = []
        self.texts: Dict[str, str | None] = {}
        self.listed = 0
        self.access = AsyncMock(return_value=ACCESS)

        def _list(assistant_id: str):
            assert assistant_id == HARNESS
            self.listed += 1
            return list(self.docs)

        def _text(doc, fmt, limit):
            return self.texts.get(doc["filename"])

        monkeypatch.setattr(f"{MODULE}.list_kb_documents", _list)
        monkeypatch.setattr(f"{MODULE}.kb_document_text", _text)
        monkeypatch.setattr(f"{MODULE}.member_emails", lambda project_id: {"u-owner": "owner@example.edu"})
        monkeypatch.setattr("apis.shared.assistants.kb_access.resolve_kb_access", self.access)

    def add(self, *docs: Dict[str, Any], **texts: str) -> None:
        self.docs.extend(docs)
        self.texts.update(texts)


@pytest.fixture
def files(monkeypatch) -> _Files:
    return _Files(monkeypatch)


def _tool(member: User = MEMBER):
    return make_project_files_tool(PROJECT, HARNESS, member)


def _call(tool, **kwargs) -> Dict[str, Any]:
    return asyncio.run(tool(**kwargs))


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    assert result["status"] == "success", result
    return result["content"][0]["json"]


def _text_of(result: Dict[str, Any]) -> str:
    assert result["status"] == "error", result
    return result["content"][0]["text"]


def _captured_users(fn) -> list:
    """Every ``User`` a function (or a function it closes over) captured."""
    found, seen = [], set()

    def walk(f):
        if id(f) in seen or not getattr(f, "__closure__", None):
            return
        seen.add(id(f))
        for cell in f.__closure__:
            value = cell.cell_contents
            if isinstance(value, User):
                found.append(value)
            elif callable(value):
                walk(value)

    walk(fn)
    return found


# ── the spec (prompt-cache contract) ────────────────────────────────────


class TestSpec:
    def test_the_spec_is_pinned(self):
        spec = _tool().tool_spec
        assert spec["name"] == "project_files"
        assert sorted(spec["inputSchema"]["json"]["properties"]) == ["filename", "offset"]
        assert "required" not in spec["inputSchema"]["json"]
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        assert digest == PINNED_SPEC

    def test_the_spec_is_the_same_for_every_project_and_member(self):
        """Nothing about the project, its files or the member reaches the spec."""
        a = make_project_files_tool("prj_a", "ast_a", MEMBER).tool_spec
        b = make_project_files_tool("prj_b", "ast_b", OTHER).tool_spec
        assert a == b

    def test_the_tool_closes_over_a_copy_of_the_member_without_the_token(self):
        from dataclasses import replace

        with_token = replace(MEMBER, raw_token="secret-token")
        tool = make_project_files_tool(PROJECT, HARNESS, with_token)
        captured = _captured_users(tool._tool_func)
        assert captured and all(u.raw_token is None and u.user_id == MEMBER.user_id for u in captured)


# ── list ────────────────────────────────────────────────────────────────


class TestList:
    def test_lists_every_file_in_name_order_with_status_size_and_who_added_it(self, files):
        files.add(
            _doc("d2", "submission-10.docx", size=2200),
            _doc("d1", "Submission-2.docx", status="processing", added_by="u-nobody"),
            _doc("d3", "rubric.pdf", content_type="application/pdf", size=900),
        )
        payload = _payload(_call(_tool()))

        assert payload["mode"] == "list"
        assert payload["count"] == 3 and payload["ready_count"] == 2
        # Case-insensitive name order (plain lexicographic: "-10" sorts before "-2"), the same every call.
        assert [f["filename"] for f in payload["files"]] == ["rubric.pdf", "submission-10.docx", "Submission-2.docx"]
        assert payload["files"][0] == {"filename": "rubric.pdf", "status": "ready", "size_bytes": 900, "added_by": "owner@example.edu"}
        assert payload["files"][2]["status"] == "not ready (processing)"
        assert "added_by" not in payload["files"][2]  # a former member: no email to show
        assert payload["truncated"] is False and payload["omitted"] == 0

    def test_an_empty_project_lists_nothing(self, files):
        payload = _payload(_call(_tool()))
        assert payload["count"] == 0 and payload["files"] == []

    def test_the_listing_is_capped_and_says_how_many_more(self, files):
        files.add(*[_doc(f"d{i:03d}", f"paper-{i:03d}.docx") for i in range(PROJECT_FILES_LIST_MAX + 25)])
        payload = _payload(_call(_tool()))

        assert payload["count"] == PROJECT_FILES_LIST_MAX + 25
        assert len(payload["files"]) == PROJECT_FILES_LIST_MAX
        assert payload["truncated"] is True and payload["omitted"] == 25
        assert payload["files"][0]["filename"] == "paper-000.docx"  # deterministic: the first by name

    def test_the_order_is_stable_across_calls(self, files):
        files.add(_doc("d1", "b.docx"), _doc("d2", "A.docx"), _doc("d3", "a.docx"))
        first = _payload(_call(_tool()))["files"]
        files.docs.reverse()
        assert _payload(_call(_tool()))["files"] == first


# ── read ────────────────────────────────────────────────────────────────


class TestRead:
    def test_reads_a_file_by_its_exact_name(self, files):
        files.add(_doc("d7", "submission-07.docx"), **{"submission-07.docx": "Submission 7\n\nThe thesis is clear."})
        payload = _payload(_call(_tool(), filename="submission-07.docx"))

        assert payload == {
            "mode": "text", "filename": "submission-07.docx", "format": "docx", "size_bytes": 1200,
            "text_bytes": len("Submission 7\n\nThe thesis is clear."),
            "content": "Submission 7\n\nThe thesis is clear.", "offset": 0, "truncated": False, "next_offset": None,
        }

    def test_a_name_matches_case_insensitively_when_no_exact_name_does(self, files):
        files.add(_doc("d7", "Submission-07.docx"), **{"Submission-07.docx": "seven"})
        payload = _payload(_call(_tool(), filename="submission-07.DOCX"))
        assert payload["filename"] == "Submission-07.docx" and payload["content"] == "seven"

    def test_an_exact_name_wins_over_a_case_insensitive_twin(self, files):
        files.add(_doc("d1", "Notes.md", content_type="text/markdown"), _doc("d2", "notes.md", content_type="text/markdown"),
                  **{"Notes.md": "capital", "notes.md": "lower"})
        assert _payload(_call(_tool(), filename="notes.md"))["content"] == "lower"

    def test_a_long_file_pages_at_the_document_read_bound(self, files):
        text = "".join(f"paragraph {i}\n" for i in range(6000))
        assert len(text.encode()) > WORKSPACE_READ_MAX_BYTES
        files.add(_doc("d1", "long.docx"), **{"long.docx": text})

        first = _payload(_call(_tool(), filename="long.docx"))
        assert first["truncated"] is True and len(first["content"].encode()) == WORKSPACE_READ_MAX_BYTES
        assert first["next_offset"] == WORKSPACE_READ_MAX_BYTES

        second = _payload(_call(_tool(), filename="long.docx", offset=first["next_offset"]))
        assert second["offset"] == WORKSPACE_READ_MAX_BYTES
        assert first["content"] + second["content"] == text
        assert second["truncated"] is False and second["next_offset"] is None

    def test_an_offset_past_the_end_is_an_error(self, files):
        files.add(_doc("d1", "short.docx"), **{"short.docx": "tiny"})
        assert "beyond the end" in _text_of(_call(_tool(), filename="short.docx", offset=99))
        assert "0 or more" in _text_of(_call(_tool(), filename="short.docx", offset=-1))

    def test_a_file_that_is_not_ready_cannot_be_read(self, files):
        files.add(_doc("d1", "draft.docx", status="processing"), **{"draft.docx": "would be readable"})
        text = _text_of(_call(_tool(), filename="draft.docx"))
        assert "not ready yet (status: processing)" in text

    def test_two_files_with_one_name_are_named_as_ambiguous(self, files):
        files.add(_doc("d1", "essay.docx", size=100, created="2026-10-01T00:00:00Z"),
                  _doc("d2", "essay.docx", size=200, created="2026-10-02T00:00:00Z"))
        text = _text_of(_call(_tool(), filename="essay.docx"))
        assert "2 files are named 'essay.docx'" in text
        assert "100 bytes, added 2026-10-01" in text and "200 bytes, added 2026-10-02" in text
        assert "rename or remove one" in text

    def test_an_unknown_name_offers_near_misses_and_the_listing(self, files):
        files.add(_doc("d1", "submission-07-final.docx"), _doc("d2", "submission-17.docx"), _doc("d3", "rubric.pdf"))
        text = _text_of(_call(_tool(), filename="submission-07"))
        assert "No file named 'submission-07'" in text
        assert "'submission-07-final.docx'" in text and "'rubric.pdf'" not in text
        assert "Call with no arguments to list them" in text

    def test_an_unknown_name_with_no_near_miss_still_points_at_the_listing(self, files):
        files.add(_doc("d1", "rubric.pdf"))
        text = _text_of(_call(_tool(), filename="zzz"))
        assert "Did you mean" not in text and "list them" in text

    def test_a_spreadsheet_is_refused_as_not_text(self, files):
        files.add(_doc("d1", "grades.xlsx", content_type=XLSX))
        text = _text_of(_call(_tool(), filename="grades.xlsx"))
        assert "'grades.xlsx' is a spreadsheet" in text and "PDF, Word, text" in text

    def test_a_scanned_or_oversized_file_is_reported_not_sampled(self, files):
        files.add(_doc("d1", "scan.pdf", content_type="application/pdf"))  # no text registered → None
        text = _text_of(_call(_tool(), filename="scan.pdf"))
        assert "'scan.pdf' has no text to read" in text


# ── access ──────────────────────────────────────────────────────────────


class TestAccess:
    def test_access_is_resolved_on_every_call_as_retrieval_resolves_it(self, files):
        files.add(_doc("d1", "a.md", content_type="text/markdown"), **{"a.md": "alpha"})
        tool = _tool()
        _call(tool)
        _call(tool, filename="a.md")

        assert files.access.await_count == 2
        files.access.assert_awaited_with(HARNESS, MEMBER.user_id, user_email=MEMBER.email)

    def test_without_a_grant_nothing_is_listed_or_read(self, files):
        files.add(_doc("d1", "a.md", content_type="text/markdown"), **{"a.md": "alpha"})
        files.access.return_value = None
        tool = _tool()

        assert _text_of(_call(tool)) == f"❌ {NO_ACCESS}"
        assert _text_of(_call(tool, filename="a.md")) == f"❌ {NO_ACCESS}"
        assert files.listed == 0

    def test_a_grant_for_another_assistant_is_not_this_one(self, files):
        files.add(_doc("d1", "a.md", content_type="text/markdown"), **{"a.md": "alpha"})
        files.access.return_value = granted("ast_other", MEMBER.user_id, "owner")
        # resolve_kb_access is asked about THIS harness; what it returns is for it. The
        # tool trusts the resolver's assistant binding, not a string it was handed.
        assert _payload(_call(_tool()))["count"] == 1

    def test_a_failure_is_an_error_result_not_an_exception(self, files, monkeypatch):
        def _boom(assistant_id):
            raise RuntimeError("DynamoDB is down")

        monkeypatch.setattr(f"{MODULE}.list_kb_documents", _boom)
        text = _text_of(_call(_tool()))
        assert "can't be read right now" in text and "DynamoDB" not in text


# ── matching, on its own ────────────────────────────────────────────────


class TestMatch:
    def test_exact_then_case_insensitive_then_candidates(self):
        docs = [_doc("d1", "Alpha.docx"), _doc("d2", "beta.docx"), _doc("d3", "gamma-notes.docx")]
        assert match_project_file(docs, "Alpha.docx")["documentId"] == "d1"
        assert match_project_file(docs, "  BETA.DOCX ")["documentId"] == "d2"
        with pytest.raises(ProjectFilesError, match="Did you mean: 'gamma-notes.docx'"):
            match_project_file(docs, "notes")

    def test_member_emails_is_best_effort(self, monkeypatch):
        class _Repo:
            def __init__(self):
                raise RuntimeError("no table")

        monkeypatch.setattr("apis.shared.projects.repository.ProjectRepository", _Repo)
        assert pf.member_emails(PROJECT) == {}
