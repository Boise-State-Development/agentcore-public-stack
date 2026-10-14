"""Folder attachments on the turn path (``docs/specs/drive-folder-attachments.md``
§4.4, PR-B): the folder ids the route honours, the cap they count against, the
one ``GetItem`` per folder, the ``document_read`` gate, and the manifest in the
message the agent receives."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List

import pytest

import apis.inference_api.chat.routes as routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.files import folder_repository
from apis.shared.files.folder_attachments import FolderAttachment, FolderCounts, FolderFileEntry

PDF = "application/pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PNG = "image/png"


def _folder(fid: str = "fa-1", session_id: str = "sess-1", files=None, **kw) -> FolderAttachment:
    files = files if files is not None else [
        FolderFileEntry(provider_file_id="d1", name="syllabus.pdf", relative_path="syllabus.pdf", mime_type=PDF,
                        size_bytes=1200, upload_id="up-1", status="attached"),
    ]
    fields = dict(folder_attach_id=fid, session_id=session_id, user_id="user-1", connector_id="google-drive",
                  connector_name="Google Drive", provider_folder_id="p", folder_name=f"Folder {fid}", status="ready",
                  counts=FolderCounts(listed=len(files), attached=len(files)), files=files)
    fields.update(kw)
    return FolderAttachment(**fields)


class FakeFolderRepo:
    def __init__(self, rows: Dict[str, Any]) -> None:
        self.rows = rows
        self.calls: List[tuple] = []
        self.threads: set = set()

    def get_sync(self, user_id: str, fid: str):
        self.calls.append((user_id, fid))
        self.threads.add(threading.current_thread().name)
        row = self.rows.get(fid)
        if isinstance(row, Exception):
            raise row
        return row


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", "true")


@pytest.fixture
def repo(monkeypatch):
    holder: Dict[str, FakeFolderRepo] = {"repo": FakeFolderRepo({})}
    monkeypatch.setattr(folder_repository, "get_folder_attachment_repository", lambda: holder["repo"])
    return holder


class _FakeResolver:
    def __init__(self, resolved=None) -> None:
        self.resolved = resolved or []
        self.calls: List[dict] = []

    async def resolve_files(self, *, user_id, upload_ids, max_files=None):  # type: ignore[no-untyped-def]
        self.calls.append({"upload_ids": list(upload_ids)})
        return self.resolved


@pytest.fixture
def phase(monkeypatch):
    state: dict = {"resolver": _FakeResolver()}

    async def _no_pending(*args, **kwargs):
        return []

    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "pop_pending_attachments", _no_pending)
    monkeypatch.setattr(routes, "get_file_resolver", lambda: state["resolver"])

    async def run(folder_ids=None, upload_ids=None, *, is_resume=False, is_continuation=False):
        req = InvocationRequest(session_id="sess-1", message="what is due?", file_upload_ids=upload_ids,
                                folder_attachment_ids=folder_ids)
        return await routes._resolve_turn_attachments(req, "user-1", None, is_resume=is_resume, is_continuation=is_continuation)

    return run, state


# ---------------------------------------------------------------------------
# Which ids the route honours
# ---------------------------------------------------------------------------


class TestRequestedFolderIds:
    def _req(self, ids):
        return InvocationRequest(session_id="s", message="m", folder_attachment_ids=ids)

    def test_ignored_while_the_flag_is_off(self, monkeypatch):
        monkeypatch.delenv("FOLDER_ATTACHMENTS_ENABLED", raising=False)
        assert routes._requested_folder_ids(self._req(["fa-1"])) == ([], 0)

    def test_deduped_in_order_and_capped_at_two(self, flag_on):
        assert routes._requested_folder_ids(self._req(["a", "b", "a", "c"])) == (["a", "b"], 1)

    def test_junk_is_dropped_before_any_key(self, flag_on):
        assert routes._requested_folder_ids(self._req(["", "x" * 65, "ok"])) == (["ok"], 0)

    def test_none_is_empty(self, flag_on):
        assert routes._requested_folder_ids(self._req(None)) == ([], 0)


# ---------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------


class TestResolveTurnFolders:
    @pytest.mark.asyncio
    async def test_one_getitem_per_folder_off_the_event_loop(self, repo):
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1"), "fa-2": _folder("fa-2")})
        folders = await routes._resolve_turn_folders("user-1", "sess-1", ["fa-1", "fa-2"])
        assert [f.folder_attach_id for f in folders] == ["fa-1", "fa-2"]
        assert repo["repo"].calls == [("user-1", "fa-1"), ("user-1", "fa-2")] or sorted(repo["repo"].calls) == [("user-1", "fa-1"), ("user-1", "fa-2")]
        assert threading.main_thread().name not in repo["repo"].threads

    @pytest.mark.asyncio
    async def test_missing_other_conversation_and_failed_reads_are_dropped(self, repo):
        repo["repo"] = FakeFolderRepo({
            "gone": None,
            "elsewhere": _folder("elsewhere", session_id="sess-OTHER"),
            "broken": RuntimeError("throttled"),
            "ok": _folder("ok"),
        })
        folders = await routes._resolve_turn_folders("user-1", "sess-1", ["gone", "elsewhere", "broken", "ok"])
        assert [f.folder_attach_id for f in folders] == ["ok"]

    @pytest.mark.asyncio
    async def test_no_ids_no_reads(self, repo):
        assert await routes._resolve_turn_folders("user-1", "sess-1", []) == []
        assert repo["repo"].calls == []


# ---------------------------------------------------------------------------
# The attachment phase (map stage B4)
# ---------------------------------------------------------------------------


class TestAttachmentPhase:
    @pytest.mark.asyncio
    async def test_a_folder_rides_the_turn_and_injects_document_read(self, flag_on, repo, phase):
        run, state = phase
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1")})
        out = await run(["fa-1"])
        assert [f.folder_attach_id for f in out.folders] == ["fa-1"]
        assert out.turn_has_document is True
        assert out.files_to_send == [] and out.marker_names == []
        assert state["resolver"].calls == []  # no upload ids, no S3

    @pytest.mark.asyncio
    async def test_an_image_or_sheet_only_folder_does_not_inject_document_read(self, flag_on, repo, phase):
        run, _ = phase
        sheet = FolderFileEntry(provider_file_id="d", name="g.xlsx", relative_path="g.xlsx", mime_type=XLSX,
                                size_bytes=10, upload_id="up-9", status="attached")
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1", files=[sheet])})
        out = await run(["fa-1"])
        assert out.turn_has_document is False
        assert routes._folders_have(out.folders, routes.is_tabular_file) is True

    @pytest.mark.asyncio
    async def test_a_folder_counts_one_against_the_cap_after_the_files(self, flag_on, repo, phase, monkeypatch):
        run, state = phase
        monkeypatch.setattr(routes, "MAX_FILES_PER_MESSAGE", 3)
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1"), "fa-2": _folder("fa-2")})
        out = await run(["fa-1", "fa-2"], upload_ids=["up-a", "up-b"])
        assert state["resolver"].calls[0]["upload_ids"] == ["up-a", "up-b"]
        assert [f.folder_attach_id for f in out.folders] == ["fa-1"]
        assert out.dropped_over_count_total == 1
        assert repo["repo"].calls == [("user-1", "fa-1")]  # the dropped folder is never read

    @pytest.mark.asyncio
    async def test_a_third_folder_is_counted_dropped(self, flag_on, repo, phase):
        run, _ = phase
        repo["repo"] = FakeFolderRepo({k: _folder(k) for k in ("a", "b", "c")})
        out = await run(["a", "b", "c"])
        assert [f.folder_attach_id for f in out.folders] == ["a", "b"]
        assert out.dropped_over_count_total == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flags", [{"is_resume": True}, {"is_continuation": True}])
    async def test_never_on_a_resume_or_continuation(self, flag_on, repo, phase, flags):
        run, _ = phase
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1")})
        out = await run(["fa-1"], **flags)
        assert out.folders == [] and repo["repo"].calls == []

    @pytest.mark.asyncio
    async def test_flag_off_reads_nothing(self, repo, phase, monkeypatch):
        monkeypatch.delenv("FOLDER_ATTACHMENTS_ENABLED", raising=False)
        run, _ = phase
        repo["repo"] = FakeFolderRepo({"fa-1": _folder("fa-1")})
        out = await run(["fa-1"])
        assert out.folders == [] and repo["repo"].calls == [] and out.turn_has_document is False

    @pytest.mark.asyncio
    async def test_folder_reads_overlap_the_upload_resolve(self, flag_on, repo, phase):
        run, state = phase
        order: List[str] = []

        class SlowRepo(FakeFolderRepo):
            def get_sync(self, user_id, fid):
                order.append("folder-start")
                import time
                time.sleep(0.05)
                order.append("folder-end")
                return _folder(fid)

        class SlowResolver(_FakeResolver):
            async def resolve_files(self, **kw):  # type: ignore[no-untyped-def]
                order.append("files-start")
                await asyncio.sleep(0.05)
                order.append("files-end")
                return []

        repo["repo"] = SlowRepo({})
        state["resolver"] = SlowResolver()
        await run(["fa-1"], upload_ids=["up-1"])
        assert order.index("files-start") < order.index("folder-end")
