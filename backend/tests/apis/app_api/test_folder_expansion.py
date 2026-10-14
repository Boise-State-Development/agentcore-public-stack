"""The folder expansion task (``docs/specs/drive-folder-attachments.md`` §4.2,
§4.3, §4.5, §4.9): walk order and paths, what is attachable, the caps, the
concurrency bound, per-file failure, the quota stop, a revoked token, resume,
and progress writes. The adapter, the row store and the upload service are
in-memory fakes; nothing here touches a provider, S3 or DynamoDB.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import pytest

from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import (
    BrowseResult,
    DownloadedFile,
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceTooLargeError,
    SourceRoot,
)
from apis.app_api.files.folders import expansion
from apis.app_api.files.folders.expansion import ExpansionDeps, attachable_mime_for, expand_folder
from apis.app_api.files.service import FileTooLargeError, QuotaExceededError
from apis.shared.files.folder_attachments import (
    FolderAttachment,
    FolderCounts,
    FolderFileEntry,
)
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.oauth.models import OAuthProviderType

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
GDOC = "application/vnd.google-apps.document"
MB = 1024 * 1024

pytestmark = pytest.mark.asyncio


def folder_entry(id: str, name: str) -> FileEntry:
    return FileEntry(id=id, name=name, type=FileEntryType.FOLDER, selectable=False)


def file_entry(id: str, name: str, mime: Optional[str] = "application/pdf", size: Optional[int] = 100, **kw) -> FileEntry:
    return FileEntry(id=id, name=name, type=FileEntryType.FILE, mime_type=mime, size_bytes=size, **kw)


class TreeAdapter(FileSourceAdapter):
    """A folder tree in memory, paged, with per-file download outcomes."""

    def __init__(self, tree: Dict[str, List[FileEntry]], *, page_size: int = 50) -> None:
        self.tree = tree
        self.page_size = page_size
        self.content: Dict[str, bytes] = {}
        self.download_errors: Dict[str, Exception] = {}
        self.browse_errors: Dict[str, Exception] = {}
        self.exports: Dict[str, DownloadedFile] = {}
        self.browse_calls: List[tuple] = []
        self.downloads: List[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.tokens_seen: set = set()

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(key="tree", display_name="Tree", icon="x", compatible_provider_types=(OAuthProviderType.GOOGLE,), required_scopes=())

    async def list_roots(self, access_token):  # type: ignore[no-untyped-def]
        return [SourceRoot(id="root", name="My Drive")]

    async def search(self, access_token, query, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def browse(self, access_token, folder_id, cursor=None):  # type: ignore[no-untyped-def]
        self.tokens_seen.add(access_token)
        self.browse_calls.append((folder_id, cursor))
        if folder_id in self.browse_errors:
            raise self.browse_errors[folder_id]
        entries = self.tree.get(folder_id, [])
        start = int(cursor) if cursor else 0
        page = entries[start:start + self.page_size]
        next_cursor = str(start + self.page_size) if start + self.page_size < len(entries) else None
        return BrowseResult(entries=page, next_cursor=next_cursor)

    async def download(self, access_token, file_id, max_bytes=None):  # type: ignore[no-untyped-def]
        self.tokens_seen.add(access_token)
        self.downloads.append(file_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.001)
            if file_id in self.download_errors:
                raise self.download_errors[file_id]
            if file_id in self.exports:
                return self.exports[file_id]
            entry = next(e for entries in self.tree.values() for e in entries if e.id == file_id)
            body = self.content.get(file_id, b"x" * (entry.size_bytes or 1))
            return DownloadedFile(content=body, filename=entry.name, content_type=entry.mime_type or "")
        finally:
            self.in_flight -= 1


class FakeRepository:
    """One row, updated in place; ``deleted`` makes every update return None."""

    def __init__(self, folder: FolderAttachment) -> None:
        self.row = folder.model_copy(deep=True)
        self.deleted = False
        self.updates: List[dict] = []

    async def update(self, user_id, folder_attach_id, **fields):  # type: ignore[no-untyped-def]
        # Snapshot what the task wrote: it mutates its entries in place after.
        self.updates.append({k: ([f.model_copy() for f in v] if k == "files" else v) for k, v in fields.items()})
        if self.deleted:
            return None
        data = self.row.model_dump()
        for key in ("status", "counts", "files", "error", "snapshot_at"):
            if fields.get(key) is not None:
                value = fields[key]
                if key == "counts":
                    value = value.model_dump()
                if key == "files":
                    value = [f.model_dump() for f in value]
                data[key] = value
        if fields.get("clear_error"):
            data["error"] = None
        self.row = FolderAttachment.model_validate(data)
        return self.row


class FakeUploadService:
    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.errors: Dict[str, Exception] = {}
        self._n = 0

    def max_download_bytes(self) -> int:
        return 50 * MB

    def max_size_for(self, filename: str, mime_type: str) -> int:
        return 25 * MB if mime_type == PPTX else 4 * MB

    async def import_file(self, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(kw)
        if kw["filename"] in self.errors:
            raise self.errors[kw["filename"]]
        self._n += 1
        return FileMetadata(
            upload_id=f"up-{self._n}", user_id=kw["user_id"], session_id=kw["session_id"],
            filename=kw["filename"], mime_type=kw["mime_type"], size_bytes=len(kw["content"]),
            s3_key="k", s3_bucket="b", status=FileStatus.READY, source=kw["source"], delivery=kw["delivery"],
        )


def _folder(**overrides) -> FolderAttachment:
    fields = dict(folder_attach_id="fa-1", session_id="sess-1", user_id="alice", connector_id="google",
                  provider_folder_id="root", folder_name="Course")
    fields.update(overrides)
    return FolderAttachment(**fields)


def _deps(adapter: TreeAdapter, repo: FakeRepository, uploads: FakeUploadService, **caps) -> ExpansionDeps:
    return ExpansionDeps(adapter=adapter, access_token="vault-token", repository=repo, upload_service=uploads, **caps)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "true")
    monkeypatch.setattr(expansion, "record_folder_expansion", _Recorder())


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[dict] = []

    def __call__(self, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(kw)


async def run(tree, *, folder=None, adapter=None, uploads=None, **caps):
    adapter = adapter or TreeAdapter(tree)
    folder = folder or _folder()
    repo = FakeRepository(folder)
    uploads = uploads or FakeUploadService()
    row = await expand_folder(folder, _deps(adapter, repo, uploads, **caps))
    return row, adapter, repo, uploads


# ---------------------------------------------------------------------------
# What is attachable
# ---------------------------------------------------------------------------


class TestAttachable:
    @pytest.mark.parametrize("entry,expected", [
        (file_entry("a", "a.pdf"), "application/pdf"),
        (file_entry("b", "b.docx", DOCX), DOCX),
        (file_entry("c", "c.csv", "text/csv"), "text/csv"),
        (file_entry("d", "d.xlsx", XLSX), XLSX),
        (file_entry("e", "e.pptx", PPTX), PPTX),
        (file_entry("f", "notes.md", "text/markdown"), "text/markdown"),
        # A Google Doc downloads as docx: the export type decides.
        (file_entry("g", "Plan", GDOC, None, download_mime_type=DOCX), DOCX),
        # The provider was vague; the extension resolves it.
        (file_entry("h", "h.pdf", "application/octet-stream"), "application/pdf"),
        (file_entry("i", "h.PDF", None), "application/pdf"),
    ])
    def test_readable_types(self, entry, expected):
        assert attachable_mime_for(entry) == expected

    @pytest.mark.parametrize("entry", [
        file_entry("a", "photo.png", "image/png"),
        file_entry("b", "photo.jpg", None),
        file_entry("c", "Form", "application/vnd.google-apps.form", selectable=False),
        file_entry("d", "clip.mp4", "video/mp4"),
        file_entry("e", "mystery", "application/octet-stream"),
        folder_entry("f", "sub"),
    ])
    def test_images_and_unreadable_types_are_not(self, entry):
        assert attachable_mime_for(entry) is None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class TestWalk:
    async def test_breadth_first_with_subfolders_paths_and_paging(self):
        tree = {
            "root": [folder_entry("w1", "week 1"), file_entry("s", "syllabus.pdf"), folder_entry("w2", "week 2"), file_entry("p", "photo.png", "image/png")],
            "w1": [file_entry("r", "rubric.docx", DOCX), folder_entry("w1a", "drafts")],
            "w1a": [file_entry("d", "draft.txt", "text/plain")],
            "w2": [file_entry("n", "notes.md", "text/markdown")],
        }
        row, adapter, repo, uploads = await run(tree, adapter=TreeAdapter(tree, page_size=2), concurrency=1)

        assert row.status == "partial"  # the photo was skipped
        assert [f.relative_path for f in row.files] == ["syllabus.pdf", "week 1/rubric.docx", "week 2/notes.md", "week 1/drafts/draft.txt"]
        assert all(f.status == "attached" and f.upload_id for f in row.files)
        assert row.counts == FolderCounts(listed=5, attached=4, skipped_type=1)
        assert adapter.downloads == ["s", "r", "n", "d"]  # listing order
        assert "p" not in adapter.downloads  # the image was never fetched
        assert adapter.browse_calls == [("root", None), ("root", "2"), ("w1", None), ("w2", None), ("w1a", None)]
        assert row.snapshot_at is not None

    async def test_every_file_attached_is_ready(self):
        row, *_ = await run({"root": [file_entry("a", "a.pdf"), file_entry("b", "b.csv", "text/csv")]})
        assert row.status == "ready" and row.error is None
        assert row.counts == FolderCounts(listed=2, attached=2)

    async def test_delivery_source_and_the_download_cap(self, monkeypatch):
        tree = {"root": [file_entry("a", "a.pdf"), file_entry("b", "b.xlsx", XLSX), file_entry("c", "c.pptx", PPTX), file_entry("d", "d.txt", "text/plain")]}
        row, adapter, _, uploads = await run(tree, concurrency=1)

        by_name = {c["filename"]: c for c in uploads.calls}
        assert by_name["a.pdf"]["delivery"] == "reference"
        assert by_name["d.txt"]["delivery"] == "reference"
        assert by_name["b.xlsx"]["delivery"] == "inline"
        assert by_name["c.pptx"]["delivery"] == "inline"
        assert {c["source"] for c in uploads.calls} == {"connector:google:folder:fa-1"}
        assert {c["user_id"] for c in uploads.calls} == {"alice"}
        assert {c["session_id"] for c in uploads.calls} == {"sess-1"}

        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "false")
        row, _, _, uploads = await run({"root": [file_entry("a", "a.pdf")]})
        assert uploads.calls[0]["delivery"] == "inline"  # no pipeline to read a reference through

    async def test_an_export_takes_its_downloaded_name(self):
        tree = {"root": [folder_entry("w1", "week 1")], "w1": [file_entry("g", "Plan", GDOC, None, download_mime_type=DOCX)]}
        adapter = TreeAdapter(tree)
        adapter.exports["g"] = DownloadedFile(content=b"docx-bytes", filename="Plan.docx", content_type=DOCX)
        row, *_ = await run(tree, adapter=adapter)
        entry = row.files[0]
        assert entry.name == "Plan.docx" and entry.relative_path == "week 1/Plan.docx"
        assert entry.mime_type == DOCX and entry.size_bytes == len(b"docx-bytes")

    async def test_the_token_reaches_the_adapter_and_nothing_else(self):
        row, adapter, repo, _ = await run({"root": [file_entry("a", "a.pdf")]})
        assert adapter.tokens_seen == {"vault-token"}
        assert "vault-token" not in str(row.model_dump()) and "vault-token" not in str(repo.updates)


# ---------------------------------------------------------------------------
# Caps (§4.5)
# ---------------------------------------------------------------------------


class TestCaps:
    async def test_max_files_truncates_the_listing(self):
        tree = {"root": [file_entry(f"f{i}", f"f{i}.pdf") for i in range(5)] + [folder_entry("sub", "sub")], "sub": [file_entry("x", "x.pdf")]}
        row, adapter, *_ = await run(tree, max_files=2)
        assert row.status == "partial" and row.counts.truncated is True
        assert [f.provider_file_id for f in row.files] == ["f0", "f1"]
        assert adapter.downloads == ["f0", "f1"]
        assert ("sub", None) not in adapter.browse_calls  # the walk stopped

    async def test_max_pages_truncates_a_sprawling_tree(self):
        tree = {"root": [folder_entry(f"d{i}", f"d{i}") for i in range(10)]}
        for i in range(10):
            tree[f"d{i}"] = [folder_entry(f"d{i}e", "empty")]
        row, adapter, *_ = await run(tree, max_pages=3)
        assert row.counts.truncated is True and len(adapter.browse_calls) == 3

    async def test_max_bytes_skips_the_rest(self):
        tree = {"root": [file_entry("a", "a.pdf", size=60), file_entry("b", "b.pdf", size=60), file_entry("c", "c.pdf", size=10)]}
        row, adapter, *_ = await run(tree, max_bytes=100, concurrency=1)
        statuses = {f.provider_file_id: f.status for f in row.files}
        assert statuses == {"a": "attached", "b": "skipped_size", "c": "attached"}
        assert row.counts.skipped_size == 1 and row.status == "partial"
        assert adapter.downloads == ["a", "c"]  # a known over-budget size is not fetched

    async def test_a_file_over_its_class_cap_is_skipped_without_a_download(self):
        tree = {"root": [file_entry("big", "big.pdf", size=5 * MB), file_entry("deck", "deck.pptx", PPTX, size=5 * MB)]}
        row, adapter, *_ = await run(tree)
        statuses = {f.provider_file_id: f.status for f in row.files}
        assert statuses == {"big": "skipped_size", "deck": "attached"}  # a deck's cap is 25 MB
        assert adapter.downloads == ["deck"]
        assert "4 MB" in row.files[0].error

    async def test_an_export_that_comes_down_too_large_is_skipped(self):
        tree = {"root": [file_entry("g", "Plan", GDOC, None, download_mime_type=DOCX)]}
        adapter = TreeAdapter(tree)
        adapter.download_errors["g"] = FileSourceTooLargeError("too big")
        row, *_ = await run(tree, adapter=adapter)
        assert row.files[0].status == "skipped_size"

        uploads = FakeUploadService()
        uploads.errors["Plan.docx"] = FileTooLargeError(5 * MB, 4 * MB)
        adapter = TreeAdapter(tree)
        adapter.exports["g"] = DownloadedFile(content=b"x", filename="Plan.docx", content_type=DOCX)
        row, *_ = await run(tree, adapter=adapter, uploads=uploads)
        assert row.files[0].status == "skipped_size"

    async def test_at_most_n_downloads_in_flight(self):
        tree = {"root": [file_entry(f"f{i}", f"f{i}.pdf") for i in range(8)]}
        row, adapter, *_ = await run(tree, concurrency=3)
        assert row.counts.attached == 8
        assert adapter.max_in_flight == 3


# ---------------------------------------------------------------------------
# Failure modes (§4.9)
# ---------------------------------------------------------------------------


class TestFailures:
    async def test_one_file_failing_does_not_stop_the_rest(self):
        tree = {"root": [file_entry("a", "a.pdf"), file_entry("b", "b.pdf"), file_entry("c", "c.pdf")]}
        adapter = TreeAdapter(tree)
        adapter.download_errors["b"] = FileSourceError("provider hiccup")
        row, *_ = await run(tree, adapter=adapter)
        statuses = {f.provider_file_id: (f.status, f.error) for f in row.files}
        assert statuses == {"a": ("attached", None), "b": ("failed", "download failed"), "c": ("attached", None)}
        assert row.status == "partial" and row.error is None
        assert row.counts.failed == 1

    async def test_a_store_failure_is_this_files_failure(self):
        uploads = FakeUploadService()
        uploads.errors["a.pdf"] = RuntimeError("s3 down")
        row, *_ = await run({"root": [file_entry("a", "a.pdf")]}, uploads=uploads)
        assert row.files[0].status == "failed" and row.files[0].error == "could not be stored"

    async def test_the_first_quota_refusal_stops_the_task_and_leaves_the_rest_pending(self):
        tree = {"root": [file_entry("a", "a.pdf"), file_entry("b", "b.pdf"), file_entry("c", "c.pdf")]}
        uploads = FakeUploadService()
        uploads.errors["b.pdf"] = QuotaExceededError(current_usage=1, max_allowed=1, required_space=1)
        row, adapter, _, uploads = await run(tree, uploads=uploads, concurrency=1)
        assert row.status == "partial" and row.error == "quota_exceeded"
        statuses = [f.status for f in row.files]
        assert statuses == ["attached", "pending", "pending"]
        assert adapter.downloads == ["a", "b"]  # c was never fetched

    async def test_a_revoked_token_during_the_listing_fails_the_row(self):
        adapter = TreeAdapter({"root": []})
        adapter.browse_errors["root"] = FileSourceAuthError("401")
        row, adapter, _, uploads = await run({}, adapter=adapter)
        assert row.status == "failed" and row.error == "needs_consent"
        assert uploads.calls == []

    async def test_a_revoked_token_during_downloads_keeps_what_was_attached(self):
        tree = {"root": [file_entry("a", "a.pdf"), file_entry("b", "b.pdf"), file_entry("c", "c.pdf")]}
        adapter = TreeAdapter(tree)
        adapter.download_errors["b"] = FileSourceAuthError("401")
        row, *_ = await run(tree, adapter=adapter, concurrency=1)
        assert row.status == "failed" and row.error == "needs_consent"
        assert [f.status for f in row.files] == ["attached", "pending", "pending"]

    async def test_a_listing_failure_mid_walk_is_partial_with_what_was_listed(self):
        tree = {"root": [file_entry("a", "a.pdf"), folder_entry("sub", "sub")], "sub": [file_entry("x", "x.pdf")]}
        adapter = TreeAdapter(tree)
        adapter.browse_errors["sub"] = FileSourceError("502")
        row, *_ = await run(tree, adapter=adapter)
        assert row.status == "partial" and row.error == "listing_failed"
        assert [f.status for f in row.files] == ["attached"]

    async def test_a_listing_failure_with_nothing_listed_is_failed(self):
        adapter = TreeAdapter({"root": []})
        adapter.browse_errors["root"] = FileSourceError("502")
        row, *_ = await run({}, adapter=adapter)
        assert row.status == "failed" and row.error == "listing_failed"

    async def test_a_row_removed_mid_task_ends_it(self):
        tree = {"root": [file_entry("a", "a.pdf")]}
        adapter, uploads = TreeAdapter(tree), FakeUploadService()
        repo = FakeRepository(_folder())
        repo.deleted = True
        assert await expand_folder(_folder(), _deps(adapter, repo, uploads)) is None
        assert adapter.downloads == []

    async def test_an_unexpected_error_marks_the_row_failed(self):
        repo = FakeRepository(_folder())
        adapter = TreeAdapter({"root": [file_entry("a", "a.pdf")]})
        adapter.browse_errors["root"] = KeyError("bug")
        assert await expand_folder(_folder(), _deps(adapter, repo, FakeUploadService())) is None
        assert repo.row.status == "failed"


# ---------------------------------------------------------------------------
# Resume (§4.9) and progress (§4.2)
# ---------------------------------------------------------------------------


class TestResumeAndProgress:
    async def test_resume_skips_files_already_attached(self):
        attached = FolderFileEntry(provider_file_id="a", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf",
                                   size_bytes=100, upload_id="up-old", status="attached")
        folder = _folder(status="expanding", error="quota_exceeded", files=[attached],
                         counts=FolderCounts(listed=2, attached=1))
        tree = {"root": [file_entry("a", "a.pdf"), file_entry("b", "b.pdf")]}
        row, adapter, _, uploads = await run(tree, folder=folder)
        assert adapter.downloads == ["b"]
        assert [(f.provider_file_id, f.status, f.upload_id) for f in row.files] == [("a", "attached", "up-old"), ("b", "attached", "up-1")]
        assert row.counts == FolderCounts(listed=2, attached=2)
        assert row.status == "ready" and row.error is None

    async def test_progress_is_written_every_n_files_and_at_the_end(self):
        tree = {"root": [file_entry(f"f{i}", f"f{i}.pdf") for i in range(5)]}
        row, _, repo, _ = await run(tree, progress_every=2, concurrency=1)
        # The listing, after files 2 and 4, and the final state.
        assert len(repo.updates) == 4
        assert repo.updates[0]["files"] and all(f.status == "pending" for f in repo.updates[0]["files"])
        assert repo.updates[0]["snapshot_at"] is not None
        assert repo.updates[1]["counts"].attached == 2
        assert repo.updates[2]["counts"].attached == 4
        assert repo.updates[3]["status"] == "ready" and repo.updates[3]["counts"].attached == 5

    async def test_the_metric_records_the_outcome_content_free(self):
        tree = {"root": [file_entry("a", "a.pdf", size=7), file_entry("p", "photo.png", "image/png")]}
        await run(tree)
        calls = expansion.record_folder_expansion.calls
        assert len(calls) == 1
        assert calls[0]["outcome"] == "partial"
        assert calls[0]["counts"] == FolderCounts(listed=2, attached=1, skipped_type=1)
        assert calls[0]["bytes_attached"] == 7
        assert calls[0]["ms"] >= 0
