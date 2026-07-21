"""Session workspace service (apis/shared/files/workspace.py).

The DynamoDB repository and S3 client are mocked; these tests pin the
contract the workspace tools rely on: metadata-table-first listing, bounded
ranged text reads with continuation, binary-by-reference, the
_store_document-style write path (READY row + quota increment), and the
fail-loudly identity / validation rules.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import apis.shared.files.workspace as workspace
from apis.shared.files.models import FileMetadata, FileStatus, UserFileQuota
from apis.shared.files.workspace import (
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceQuotaExceededError,
    WorkspaceValidationError,
    list_workspace_files,
    read_workspace_file,
    write_workspace_file,
)

USER = "user-1"
SESSION = "sess-1"


def _meta(
    upload_id: str = "f1",
    user_id: str = USER,
    session_id: str = SESSION,
    filename: str = "notes.md",
    mime_type: str = "text/markdown",
    size_bytes: int = 10,
    status: FileStatus = FileStatus.READY,
    source: str = "upload",
) -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id,
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        s3_key=f"user-files/{user_id}/{session_id}/{upload_id}/{filename}",
        s3_bucket="bucket",
        status=status,
        source=source,
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )


@pytest.fixture
def repo(monkeypatch) -> MagicMock:
    repo = MagicMock()
    repo.list_session_files = AsyncMock(return_value=[])
    repo.list_user_files = AsyncMock(return_value=([], None))
    repo.get_file = AsyncMock(return_value=None)
    repo.create_file = AsyncMock()
    repo.increment_quota = AsyncMock()
    repo.get_user_quota = AsyncMock(return_value=UserFileQuota(user_id=USER))
    monkeypatch.setattr(workspace, "get_file_upload_repository", lambda: repo)
    return repo


@pytest.fixture
def s3(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://signed.example/f"
    monkeypatch.setattr(workspace, "_s3", lambda: client)
    monkeypatch.setenv("S3_USER_FILES_BUCKET_NAME", "bucket")
    return client


def _s3_body(data: bytes) -> dict:
    body = MagicMock()
    body.read.return_value = data
    return {"Body": body}


class TestListWorkspaceFiles:
    @pytest.mark.asyncio
    async def test_session_scope_filters_ownership(self, repo, s3):
        repo.list_session_files.return_value = [
            _meta(upload_id="mine"),
            _meta(upload_id="theirs", user_id="someone-else"),
        ]
        result = await list_workspace_files(USER, SESSION)
        assert [f["upload_id"] for f in result["files"]] == ["mine"]
        assert result["scope"] == "session"
        assert result["truncated"] is False
        repo.list_session_files.assert_awaited_once_with(
            SESSION, status=FileStatus.READY
        )

    @pytest.mark.asyncio
    async def test_entry_shape_and_readable_flag(self, repo, s3):
        repo.list_session_files.return_value = [
            _meta(upload_id="t", mime_type="text/plain"),
            _meta(upload_id="j", mime_type="application/json"),
            _meta(upload_id="b", filename="deck.pdf", mime_type="application/pdf"),
        ]
        result = await list_workspace_files(USER, SESSION)
        by_id = {f["upload_id"]: f for f in result["files"]}
        assert by_id["t"]["readable"] and by_id["j"]["readable"]
        assert not by_id["b"]["readable"]
        assert by_id["t"]["source"] == "upload"
        # session scope omits session_id (it's implied)
        assert "session_id" not in by_id["t"]

    @pytest.mark.asyncio
    async def test_user_scope_includes_session_and_truncation(self, repo, s3):
        repo.list_user_files.return_value = ([_meta()], "next-cursor")
        result = await list_workspace_files(USER, SESSION, scope="user")
        assert result["truncated"] is True
        assert result["files"][0]["session_id"] == SESSION
        repo.list_user_files.assert_awaited_once_with(
            USER, limit=workspace.WORKSPACE_LIST_MAX_ENTRIES, status=FileStatus.READY
        )

    @pytest.mark.asyncio
    async def test_session_scope_caps_entries(self, repo, s3, monkeypatch):
        monkeypatch.setattr(workspace, "WORKSPACE_LIST_MAX_ENTRIES", 2)
        repo.list_session_files.return_value = [
            _meta(upload_id=f"f{i}") for i in range(4)
        ]
        result = await list_workspace_files(USER, SESSION)
        assert result["count"] == 2 and result["truncated"] is True

    @pytest.mark.asyncio
    async def test_unknown_scope_rejected(self, repo, s3):
        with pytest.raises(WorkspaceValidationError):
            await list_workspace_files(USER, SESSION, scope="everything")

    @pytest.mark.asyncio
    async def test_missing_identity_fails_loudly(self, repo, s3):
        with pytest.raises(WorkspaceError):
            await list_workspace_files("", SESSION)
        with pytest.raises(WorkspaceError):
            await list_workspace_files(USER, "")


class TestReadWorkspaceFile:
    @pytest.mark.asyncio
    async def test_text_read_full(self, repo, s3):
        repo.get_file.return_value = _meta(size_bytes=5)
        s3.get_object.return_value = _s3_body(b"hello")
        result = await read_workspace_file(USER, "f1")
        assert result["encoding"] == "text"
        assert result["content"] == "hello"
        assert result["truncated"] is False and result["next_offset"] is None
        # ranged GET, never a full-object read
        assert "Range" in s3.get_object.call_args.kwargs

    @pytest.mark.asyncio
    async def test_text_read_truncates_with_continuation(self, repo, s3, monkeypatch):
        monkeypatch.setattr(workspace, "WORKSPACE_READ_MAX_BYTES", 4)
        repo.get_file.return_value = _meta(size_bytes=10)
        s3.get_object.return_value = _s3_body(b"abcd")
        result = await read_workspace_file(USER, "f1")
        assert result["truncated"] is True and result["next_offset"] == 4
        assert s3.get_object.call_args.kwargs["Range"] == "bytes=0-3"

    @pytest.mark.asyncio
    async def test_text_read_offset_continues(self, repo, s3, monkeypatch):
        monkeypatch.setattr(workspace, "WORKSPACE_READ_MAX_BYTES", 4)
        repo.get_file.return_value = _meta(size_bytes=6)
        s3.get_object.return_value = _s3_body(b"ef")
        result = await read_workspace_file(USER, "f1", offset=4)
        assert result["content"] == "ef"
        assert result["truncated"] is False and result["next_offset"] is None
        assert s3.get_object.call_args.kwargs["Range"] == "bytes=4-7"

    @pytest.mark.asyncio
    async def test_binary_returns_reference_never_content(self, repo, s3):
        repo.get_file.return_value = _meta(
            filename="deck.pdf", mime_type="application/pdf"
        )
        result = await read_workspace_file(USER, "f1")
        assert result["encoding"] == "reference"
        assert result["download_url"] == "https://signed.example/f"
        assert "content" not in result
        s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_and_non_ready_are_not_found(self, repo, s3):
        repo.get_file.return_value = None
        with pytest.raises(WorkspaceFileNotFoundError):
            await read_workspace_file(USER, "ghost")
        repo.get_file.return_value = _meta(status=FileStatus.PENDING)
        with pytest.raises(WorkspaceFileNotFoundError):
            await read_workspace_file(USER, "f1")

    @pytest.mark.asyncio
    async def test_offset_beyond_end_rejected(self, repo, s3):
        repo.get_file.return_value = _meta(size_bytes=5)
        with pytest.raises(WorkspaceValidationError):
            await read_workspace_file(USER, "f1", offset=5)
        with pytest.raises(WorkspaceValidationError):
            await read_workspace_file(USER, "f1", offset=-1)


class TestWriteWorkspaceFile:
    @pytest.mark.asyncio
    async def test_happy_path_follows_store_document_contract(self, repo, s3):
        result = await write_workspace_file(
            USER, SESSION, "report.md", "# Hi", mime_type="text/markdown"
        )

        put_kwargs = s3.put_object.call_args.kwargs
        assert put_kwargs["Bucket"] == "bucket"
        assert put_kwargs["ContentType"] == "text/markdown"
        assert put_kwargs["Key"].startswith(f"user-files/{USER}/{SESSION}/")
        assert put_kwargs["Key"].endswith("/report.md")

        created: FileMetadata = repo.create_file.call_args.args[0]
        assert created.source == "agent"
        status = created.status if isinstance(created.status, str) else created.status.value
        assert status == FileStatus.READY.value
        repo.increment_quota.assert_awaited_once_with(USER, len(b"# Hi"))

        assert result["filename"] == "report.md"
        assert result["download_url"] == "https://signed.example/f"
        assert result["size_bytes"] == 4

    @pytest.mark.asyncio
    async def test_extension_appended_when_missing(self, repo, s3):
        result = await write_workspace_file(
            USER, SESSION, "report", "x", mime_type="text/markdown"
        )
        assert result["filename"] == "report.md"

    @pytest.mark.asyncio
    async def test_extension_mismatch_rejected(self, repo, s3):
        with pytest.raises(WorkspaceValidationError):
            await write_workspace_file(
                USER, SESSION, "report.csv", "x", mime_type="text/markdown"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mime", ["application/pdf", "image/png", "junk"])
    async def test_non_text_mime_rejected(self, repo, s3, mime):
        with pytest.raises(WorkspaceValidationError):
            await write_workspace_file(USER, SESSION, "f.bin", "x", mime_type=mime)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["../evil.txt", "a/b.txt", "a\\b.txt", "", ".hidden"]
    )
    async def test_bad_filenames_rejected(self, repo, s3, name):
        with pytest.raises(WorkspaceValidationError):
            await write_workspace_file(USER, SESSION, name, "x")

    @pytest.mark.asyncio
    async def test_oversized_content_rejected(self, repo, s3, monkeypatch):
        monkeypatch.setattr(workspace, "WORKSPACE_WRITE_MAX_BYTES", 8)
        with pytest.raises(WorkspaceValidationError):
            await write_workspace_file(USER, SESSION, "big.txt", "123456789")
        s3.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_quota_exceeded_rejected_before_write(self, repo, s3, monkeypatch):
        monkeypatch.setattr(workspace, "_USER_QUOTA_BYTES", 10)
        repo.get_user_quota.return_value = UserFileQuota(user_id=USER, total_bytes=9)
        with pytest.raises(WorkspaceQuotaExceededError):
            await write_workspace_file(USER, SESSION, "f.txt", "abc")
        s3.put_object.assert_not_called()
        repo.create_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_identity_fails_loudly(self, repo, s3):
        with pytest.raises(WorkspaceError):
            await write_workspace_file("", SESSION, "f.txt", "x")
        with pytest.raises(WorkspaceError):
            await write_workspace_file(USER, "", "f.txt", "x")
