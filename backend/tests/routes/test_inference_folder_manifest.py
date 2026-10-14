"""End to end on ``POST /invocations``: an attached folder's manifest and its
guidance follow the user's text in the message the agent receives
(``docs/specs/drive-folder-attachments.md`` §4.4, PR-B)."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.files import folder_repository
from apis.shared.files.folder_attachments import FolderAttachment, FolderCounts, FolderFileEntry


def _row(session_id: str) -> FolderAttachment:
    files = [
        FolderFileEntry(provider_file_id="d1", name="syllabus.pdf", relative_path="syllabus.pdf",
                        mime_type="application/pdf", size_bytes=1200, upload_id="up-1", status="attached"),
        FolderFileEntry(provider_file_id="d2", name="rubric.docx", relative_path="week 1/rubric.docx",
                        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        size_bytes=86_016, upload_id="up-2", status="attached"),
    ]
    return FolderAttachment(folder_attach_id="fa-1", session_id=session_id, user_id="user-1",
                            connector_id="google-drive", connector_name="Google Drive", provider_folder_id="p",
                            folder_name="BIO 101", status="ready", counts=FolderCounts(listed=2, attached=2), files=files)


class _Repo:
    def __init__(self, rows: Dict[str, Any]) -> None:
        self.rows = rows
        self.calls: List[tuple] = []

    def get_sync(self, user_id, fid):  # type: ignore[no-untyped-def]
        self.calls.append((user_id, fid))
        return self.rows.get(fid)


def _post(make_user, monkeypatch, *, flag: str, body: dict):
    monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", flag)
    repo = _Repo({"fa-1": _row(body["session_id"])})
    monkeypatch.setattr(folder_repository, "get_folder_attachment_repository", lambda: repo)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user_trusted] = lambda: make_user(user_id="user-1", raw_token="t")

    captured: dict = {}
    agent = MagicMock()

    async def fake_stream(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    with patch.object(routes, "get_agent", return_value=agent), patch.object(
        routes, "is_quota_enforcement_enabled", return_value=False
    ):
        resp = TestClient(app).post("/invocations", json=body)
        _ = resp.text
    return resp, captured.get("prompt"), repo


def _text(prompt) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(b.get("text", "") for b in prompt if isinstance(b, dict))


class TestManifestReachesTheAgent:
    def test_the_manifest_and_guidance_follow_the_message(self, make_user, monkeypatch):
        resp, prompt, repo = _post(make_user, monkeypatch, flag="true", body={
            "session_id": "sess-folder-1", "message": "what is due?", "folder_attachment_ids": ["fa-1"],
        })
        assert resp.status_code == 200
        text = _text(prompt)
        # The user's text first, then the manifest (a MagicMock agent also makes
        # the MCP-Apps context drain prepend a block, which is not under test).
        assert text.index("what is due?") < text.index("<folder ")
        assert (
            '<folder name="BIO 101" source="Google Drive" status="ready" files="2">\n'
            "  syllabus.pdf — upload_id=up-1 — 1 KB — pdf\n"
            "  week 1/rubric.docx — upload_id=up-2 — 84 KB — docx\n"
            "</folder>"
        ) in text
        assert "_The folder's files are attached" in text and "`document_read`" in text
        assert repo.calls == [("user-1", "fa-1")]

    def test_flag_off_the_field_is_ignored(self, make_user, monkeypatch):
        resp, prompt, repo = _post(make_user, monkeypatch, flag="", body={
            "session_id": "sess-folder-2", "message": "what is due?", "folder_attachment_ids": ["fa-1"],
        })
        assert resp.status_code == 200
        assert "<folder" not in _text(prompt) and repo.calls == []

    def test_a_turn_without_folders_reads_no_folder_row(self, make_user, monkeypatch):
        resp, prompt, repo = _post(make_user, monkeypatch, flag="true", body={
            "session_id": "sess-folder-3", "message": "hello",
        })
        assert resp.status_code == 200
        assert "<folder" not in _text(prompt) and repo.calls == []
