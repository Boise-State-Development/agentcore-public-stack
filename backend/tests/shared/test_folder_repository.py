"""Folder attachment rows in the user-files table (moto), and the ``FILE#``
isolation that keeps them out of the file listings."""

from __future__ import annotations

import pytest

from apis.shared.files.folder_attachments import FolderAttachment, FolderCounts, FolderFileEntry
from apis.shared.files.folder_repository import FolderAttachmentRepository
from apis.shared.files.models import FileMetadata, FileStatus


def _folder(folder_attach_id="fa-1", session_id="s1", user_id="u1", **kw) -> FolderAttachment:
    fields = dict(
        folder_attach_id=folder_attach_id, session_id=session_id, user_id=user_id,
        connector_id="google", provider_folder_id="drive-1", folder_name="Course",
    )
    fields.update(kw)
    return FolderAttachment(**fields)


def _file(upload_id="f1", session_id="s1", user_id="u1") -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id, user_id=user_id, session_id=session_id, filename="a.pdf",
        mime_type="application/pdf", size_bytes=10, s3_key=f"user-files/{user_id}/{session_id}/{upload_id}/a.pdf",
        s3_bucket="b", status=FileStatus.READY,
    )


@pytest.fixture()
def folder_repository(files_table):
    return FolderAttachmentRepository(table_name="test-user-files")


class TestFolderAttachmentRepository:
    @pytest.mark.asyncio
    async def test_create_get_delete(self, folder_repository):
        await folder_repository.create(_folder())
        row = await folder_repository.get("u1", "fa-1")
        assert row is not None and row.folder_name == "Course" and row.status == "expanding"
        assert await folder_repository.get("u2", "fa-1") is None  # owner-scoped
        deleted = await folder_repository.delete("u1", "fa-1")
        assert deleted is not None and deleted.folder_attach_id == "fa-1"
        assert await folder_repository.get("u1", "fa-1") is None
        assert await folder_repository.delete("u1", "fa-1") is None

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, folder_repository):
        await folder_repository.create(_folder())
        with pytest.raises(ValueError):
            await folder_repository.create(_folder())

    @pytest.mark.asyncio
    async def test_update_sets_fields_and_touches_updated_at(self, folder_repository):
        created = await folder_repository.create(_folder())
        files = [FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf",
                                 size_bytes=5, upload_id="up-1", status="attached")]
        row = await folder_repository.update(
            "u1", "fa-1", status="partial", counts=FolderCounts(listed=2, attached=1, truncated=True),
            files=files, error="quota_exceeded", snapshot_at="2026-10-13T10:00:00Z",
        )
        assert row.status == "partial" and row.error == "quota_exceeded"
        assert row.counts.listed == 2 and row.counts.truncated is True
        assert row.files == files
        assert row.snapshot_at == "2026-10-13T10:00:00Z"
        assert row.updated_at >= created.updated_at
        assert row.folder_name == "Course"  # untouched fields stay

        cleared = await folder_repository.update("u1", "fa-1", status="ready", clear_error=True)
        assert cleared.error is None and cleared.status == "ready"
        assert cleared.files == files

    @pytest.mark.asyncio
    async def test_update_of_a_missing_row_is_none(self, folder_repository):
        assert await folder_repository.update("u1", "nope", status="ready") is None

    @pytest.mark.asyncio
    async def test_list_and_delete_by_session(self, folder_repository):
        await folder_repository.create(_folder("fa-1", session_id="s1"))
        await folder_repository.create(_folder("fa-2", session_id="s1"))
        await folder_repository.create(_folder("fa-3", session_id="s2"))
        assert {f.folder_attach_id for f in await folder_repository.list_session_folders("s1")} == {"fa-1", "fa-2"}
        deleted = await folder_repository.delete_session_folders("s1")
        assert {f.folder_attach_id for f in deleted} == {"fa-1", "fa-2"}
        assert await folder_repository.list_session_folders("s1") == []
        assert len(await folder_repository.list_session_folders("s2")) == 1


class TestFolderRowsStayOutOfFileListings:
    @pytest.mark.asyncio
    async def test_session_file_listing_and_stats_skip_folder_rows(self, file_repository, folder_repository):
        await file_repository.create_file(_file("f1"))
        await folder_repository.create(_folder("fa-1"))

        files = await file_repository.list_session_files("s1")
        assert [f.upload_id for f in files] == ["f1"]
        assert [f.upload_id for f in file_repository.list_session_files_sync("s1", status=FileStatus.READY)] == ["f1"]
        stats = await file_repository.list_session_file_stats("s1")
        assert [s["uploadId"] for s in stats] == ["f1"]
        # And the folder listing sees only its own rows.
        assert [f.folder_attach_id for f in await folder_repository.list_session_folders("s1")] == ["fa-1"]

    @pytest.mark.asyncio
    async def test_user_file_listing_skips_folder_rows(self, file_repository, folder_repository):
        await file_repository.create_file(_file("f1"))
        await folder_repository.create(_folder("fa-1"))
        files, _ = await file_repository.list_user_files("u1")
        assert [f.upload_id for f in files] == ["f1"]


class TestRefreshFields:
    @pytest.mark.asyncio
    async def test_refresh_state_and_entry_stamps_round_trip(self, folder_repository):
        from apis.shared.files.folder_attachments import RefreshSummary

        await folder_repository.create(_folder())
        files = [FolderFileEntry(provider_file_id="d1", name="a.pdf", relative_path="a.pdf", mime_type="application/pdf",
                                 size_bytes=3, upload_id="up-1", status="attached", etag="v2", content_hash="abc",
                                 removed_from_source=True)]
        row = await folder_repository.update(
            "u1", "fa-1", status="expanding", refreshing=True, files=files,
        )
        assert row.refreshing is True
        row = await folder_repository.update(
            "u1", "fa-1", status="ready", refreshing=False, refreshed_at="2026-10-14T16:00:00Z",
            last_refresh=RefreshSummary(unchanged=1, updated=2, added=3, removed_from_source=1, failed=0),
        )
        assert row.refreshing is False and row.refreshed_at == "2026-10-14T16:00:00Z"
        assert row.last_refresh.updated == 2 and row.last_refresh.removed_from_source == 1
        entry = (await folder_repository.get("u1", "fa-1")).files[0]
        assert (entry.etag, entry.content_hash, entry.removed_from_source) == ("v2", "abc", True)


class TestFileContentReplace:
    @pytest.mark.asyncio
    async def test_new_size_and_the_old_derivatives_dropped(self, file_repository):
        meta = _file("f1").model_copy(update={"digest": {"abstract": "old"}, "preparation": {"status": "ready"}})
        await file_repository.create_file(meta)
        updated = await file_repository.replace_file_content_fields("u1", "f1", 99)
        assert updated.size_bytes == 99 and updated.digest is None and updated.preparation is None
        assert updated.upload_id == "f1" and updated.s3_key == meta.s3_key

    @pytest.mark.asyncio
    async def test_a_missing_row_is_none(self, file_repository):
        assert await file_repository.replace_file_content_fields("u1", "nope", 1) is None

    @pytest.mark.asyncio
    async def test_quota_bytes_move_without_counting_a_file(self, file_repository):
        await file_repository.increment_quota("u1", 100)
        quota = await file_repository.adjust_quota_bytes("u1", 25)
        assert (quota.total_bytes, quota.file_count) == (125, 1)
        quota = await file_repository.adjust_quota_bytes("u1", -50)
        assert (quota.total_bytes, quota.file_count) == (75, 1)
