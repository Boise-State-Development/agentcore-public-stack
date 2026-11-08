"""``knowledge_files``: the ``project_files`` tool in an ordinary Agent's voice.

The behaviour shared by both voices (matching, paging, bounds, not-ready
handling) is covered by ``test_project_files_tool.py``; here, what differs: the
spec, the listing (a type, and no ``added_by``), the words of every refusal, and
that access is resolved per call against the Agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from agents.builtin_tools.project_files_tool import (
    KNOWLEDGE_FILES_TOOL_NAME,
    KNOWLEDGE_NO_ACCESS,
    PROJECT_FILES_LIST_MAX,
    make_knowledge_files_tool,
    make_project_files_tool,
)
from apis.shared.assistants.kb_access import granted
from apis.shared.auth.models import User

MODULE = "agents.builtin_tools.project_files_tool"
AGENT = "ast_agent_1"
USER = User(user_id="u-user", email="user@example.edu", name="Use", roles=["default"])
OTHER = User(user_id="u-other", email="other@example.edu", name="Oth", roles=["default"])
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The spec's digest. It is in the cached ``toolConfig`` prefix of every Agent
#: conversation while ``AGENT_KNOWLEDGE_FILES_ENABLED`` is on: change it on
#: purpose, and say so in the PR.
PINNED_SPEC = "0d4dad2246a5d10e66f9a93feb12abd93184110323ab5d6899b4f2a4633e7c8b"


def _doc(doc_id: str, filename: str, *, status: str = "complete", size: int = 1200, content_type: str = DOCX,
         created: str = "2026-10-01T00:00:00Z") -> Dict[str, Any]:
    return {
        "documentId": doc_id, "assistantId": AGENT, "filename": filename, "contentType": content_type,
        "sizeBytes": size, "s3Key": f"assistants/{AGENT}/documents/{doc_id}/{filename}", "status": status,
        "createdAt": created, "updatedAt": created, "addedByUserId": "u-author",
    }


class _Knowledge:
    def __init__(self, monkeypatch) -> None:
        self.docs: List[Dict[str, Any]] = []
        self.texts: Dict[str, str | None] = {}
        self.access = AsyncMock(return_value=granted(AGENT, USER.user_id, "viewer"))
        monkeypatch.setattr(f"{MODULE}.list_kb_documents", lambda assistant_id: list(self.docs))
        monkeypatch.setattr(f"{MODULE}.kb_document_text", lambda doc, fmt, limit: self.texts.get(doc["filename"]))

        def _no_emails(project_id):
            raise AssertionError("an Agent's listing never looks up who added a document")

        monkeypatch.setattr(f"{MODULE}.member_emails", _no_emails)
        monkeypatch.setattr("apis.shared.assistants.kb_access.resolve_kb_access", self.access)

    def add(self, *docs: Dict[str, Any], **texts: str) -> None:
        self.docs.extend(docs)
        self.texts.update(texts)


@pytest.fixture
def knowledge(monkeypatch) -> _Knowledge:
    return _Knowledge(monkeypatch)


def _call(**kwargs) -> Dict[str, Any]:
    return asyncio.run(make_knowledge_files_tool(AGENT, USER)(**kwargs))


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    assert result["status"] == "success", result
    return result["content"][0]["json"]


def _error(result: Dict[str, Any]) -> str:
    assert result["status"] == "error", result
    return result["content"][0]["text"]


class TestSpec:
    def test_the_spec_is_pinned(self):
        spec = make_knowledge_files_tool(AGENT, USER).tool_spec
        assert spec["name"] == KNOWLEDGE_FILES_TOOL_NAME == "knowledge_files"
        assert sorted(spec["inputSchema"]["json"]["properties"]) == ["filename", "offset"]
        assert "required" not in spec["inputSchema"]["json"]
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        assert digest == PINNED_SPEC

    def test_the_spec_is_the_same_for_every_agent_and_user(self):
        assert make_knowledge_files_tool("ast_a", USER).tool_spec == make_knowledge_files_tool("ast_b", OTHER).tool_spec

    def test_it_speaks_of_knowledge_not_project_files(self):
        spec = make_knowledge_files_tool(AGENT, USER).tool_spec
        assert "Knowledge" in spec["description"] and "project" not in spec["description"].lower()
        # The 2026-11 review: an Agent searched the user's Drive for a Knowledge file.
        assert "not in the user's" in spec["description"]

    def test_the_two_voices_are_distinct_tools(self):
        project = make_project_files_tool("prj_1", AGENT, USER).tool_spec
        assert project["name"] == "project_files"
        assert project["description"] != make_knowledge_files_tool(AGENT, USER).tool_spec["description"]
        knowledge = make_knowledge_files_tool(AGENT, USER).tool_spec["inputSchema"]["json"]["properties"]
        assert sorted(project["inputSchema"]["json"]["properties"]) == sorted(knowledge)


class TestList:
    def test_lists_every_document_with_its_type_and_no_author(self, knowledge):
        knowledge.add(
            _doc("d1", "Process.txt", content_type="text/plain", size=900),
            _doc("d2", "budget-template.xlsx", content_type=XLSX),
            _doc("d3", "guide.docx", status="processing"),
        )
        payload = _payload(_call())

        assert payload["count"] == 3 and payload["ready_count"] == 2
        assert payload["files"] == [
            {"filename": "budget-template.xlsx", "type": "spreadsheet", "status": "ready", "size_bytes": 1200},
            {"filename": "guide.docx", "type": "docx", "status": "not ready (processing)", "size_bytes": 1200},
            {"filename": "Process.txt", "type": "txt", "status": "ready", "size_bytes": 900},
        ]

    def test_the_listing_is_bounded_like_project_files(self, knowledge):
        knowledge.add(*[_doc(f"d{i:03d}", f"doc-{i:03d}.docx") for i in range(PROJECT_FILES_LIST_MAX + 3)])
        payload = _payload(_call())
        assert len(payload["files"]) == PROJECT_FILES_LIST_MAX and payload["omitted"] == 3


class TestRead:
    def test_reads_a_document_by_name(self, knowledge):
        knowledge.add(_doc("d1", "Process.txt", content_type="text/plain"), **{"Process.txt": "Step 1."})
        payload = _payload(_call(filename="process.txt"))
        assert payload["mode"] == "text" and payload["filename"] == "Process.txt" and payload["content"] == "Step 1."

    def test_an_unknown_name_says_knowledge_is_not_in_the_users_storage(self, knowledge):
        knowledge.add(_doc("d1", "Process.txt", content_type="text/plain"))
        text = _error(_call(filename="Process"))
        assert "No document named 'Process' in this assistant's Knowledge" in text
        assert "Did you mean: 'Process.txt'" in text
        assert "not in the user's Drive" in text

    def test_two_documents_with_one_name_point_at_the_author(self, knowledge):
        knowledge.add(_doc("d1", "a.docx"), _doc("d2", "a.docx"))
        text = _error(_call(filename="a.docx"))
        assert "2 documents are named 'a.docx'" in text and "assistant's author" in text
        assert "Files page" not in text and "member" not in text

    def test_a_failed_document_points_at_the_agent_designer(self, knowledge):
        knowledge.add(_doc("d1", "broken.pdf", status="failed"))
        payload = _payload(_call(filename="broken.pdf"))
        assert payload["mode"] == "not_ready" and "Agent Designer" in payload["message"]
        assert "project" not in payload["message"]

    def test_a_spreadsheet_is_refused_as_not_text(self, knowledge):
        knowledge.add(_doc("d1", "budget-template.xlsx", content_type=XLSX))
        assert "is a spreadsheet" in _error(_call(filename="budget-template.xlsx"))


class TestAccess:
    def test_access_is_resolved_on_every_call_against_the_agent(self, knowledge):
        knowledge.add(_doc("d1", "a.md", content_type="text/markdown"), **{"a.md": "alpha"})
        _call()
        _call(filename="a.md")
        assert knowledge.access.await_count == 2
        knowledge.access.assert_awaited_with(AGENT, USER.user_id, user_email=USER.email)

    def test_without_a_grant_nothing_is_listed_or_read(self, knowledge):
        knowledge.add(_doc("d1", "a.md", content_type="text/markdown"), **{"a.md": "alpha"})
        knowledge.access.return_value = None
        assert _error(_call()) == f"❌ {KNOWLEDGE_NO_ACCESS}"
        assert _error(_call(filename="a.md")) == f"❌ {KNOWLEDGE_NO_ACCESS}"

    def test_a_failure_is_an_error_result_in_the_knowledge_voice(self, knowledge, monkeypatch):
        def _boom(assistant_id):
            raise RuntimeError("DynamoDB is down")

        monkeypatch.setattr(f"{MODULE}.list_kb_documents", _boom)
        text = _error(_call())
        assert "Knowledge can't be read right now" in text and "DynamoDB" not in text
