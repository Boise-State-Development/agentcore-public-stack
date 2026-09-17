"""Task 8: Files repository (moto DynamoDB) + file resolver (moto S3)."""

import base64
import pytest
from datetime import datetime
from apis.shared.files.models import FileMetadata, FileStatus, UserFileQuota


def _make_file(upload_id="f1", user_id="u1", session_id="s1", **kw):
    defaults = dict(
        upload_id=upload_id, user_id=user_id, session_id=session_id,
        filename="test.pdf", mime_type="application/pdf", size_bytes=1024,
        s3_key=f"uploads/{user_id}/{upload_id}", s3_bucket="test-file-uploads",
        status=FileStatus.READY,
    )
    defaults.update(kw)
    return FileMetadata(**defaults)


class TestFileUploadRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, file_repository):
        fm = _make_file()
        created = await file_repository.create_file(fm)
        assert created.upload_id == "f1"
        result = await file_repository.get_file("u1", "f1")
        assert result is not None
        assert result.filename == "test.pdf"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, file_repository):
        assert await file_repository.get_file("u1", "nope") is None

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, file_repository):
        await file_repository.create_file(_make_file())
        with pytest.raises(Exception):
            await file_repository.create_file(_make_file())

    @pytest.mark.asyncio
    async def test_update_status(self, file_repository):
        await file_repository.create_file(_make_file(status=FileStatus.PENDING))
        updated = await file_repository.update_file_status("u1", "f1", FileStatus.READY)
        assert updated is not None
        assert updated.status == "ready"

    @pytest.mark.asyncio
    async def test_update_digest_round_trips_and_needs_the_row(self, file_repository):
        await file_repository.create_file(_make_file())
        digest = {"version": 1, "status": "ready", "format": "pdf", "count": 3,
                  "sections": [{"start": 1, "title": "Intro"}], "tokens": 120, "abstract": "x"}
        updated = await file_repository.update_file_digest("u1", "f1", digest)
        assert updated is not None and updated.digest["count"] == 3
        assert (await file_repository.get_file("u1", "f1")).digest["sections"] == [{"start": 1, "title": "Intro"}]
        assert await file_repository.update_file_digest("u1", "missing", digest) is None
        # Rows without a digest read as None, never as an empty map.
        await file_repository.create_file(_make_file("f2"))
        assert (await file_repository.get_file("u1", "f2")).digest is None

    @pytest.mark.asyncio
    async def test_delete_file(self, file_repository):
        await file_repository.create_file(_make_file())
        deleted = await file_repository.delete_file("u1", "f1")
        assert deleted is not None
        assert await file_repository.get_file("u1", "f1") is None

    @pytest.mark.asyncio
    async def test_list_user_files(self, file_repository):
        await file_repository.create_file(_make_file("f1"))
        await file_repository.create_file(_make_file("f2"))
        files, _ = await file_repository.list_user_files("u1")
        assert len(files) == 2

    @pytest.mark.asyncio
    async def test_list_session_files(self, file_repository):
        await file_repository.create_file(_make_file("f1", session_id="s1"))
        await file_repository.create_file(_make_file("f2", session_id="s1"))
        await file_repository.create_file(_make_file("f3", session_id="s2"))
        files = await file_repository.list_session_files("s1")
        assert len(files) == 2

    @pytest.mark.asyncio
    async def test_quota_default(self, file_repository):
        quota = await file_repository.get_user_quota("u1")
        assert quota.total_bytes == 0
        assert quota.file_count == 0

    @pytest.mark.asyncio
    async def test_increment_quota(self, file_repository):
        quota = await file_repository.increment_quota("u1", 1024)
        assert quota.total_bytes == 1024
        assert quota.file_count == 1

    @pytest.mark.asyncio
    async def test_decrement_quota(self, file_repository):
        await file_repository.increment_quota("u1", 2048)
        quota = await file_repository.decrement_quota("u1", 1024)
        assert quota.total_bytes == 1024

    @pytest.mark.asyncio
    async def test_delete_session_files(self, file_repository):
        await file_repository.create_file(_make_file("f1", session_id="s1"))
        await file_repository.create_file(_make_file("f2", session_id="s1"))
        deleted = await file_repository.delete_session_files("s1")
        assert len(deleted) == 2


class TestFileResolver:
    @pytest.mark.asyncio
    async def test_resolve_files(self, file_repository, s3_bucket, aws):
        import boto3
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(Bucket=s3_bucket, Key="uploads/u1/f1", Body=b"PDF content here")
        await file_repository.create_file(_make_file())

        from apis.shared.files.file_resolver import FileResolver
        resolver = FileResolver(s3_client=s3)
        resolver._file_repository = file_repository
        files = await resolver.resolve_files("u1", ["f1"])
        assert len(files) == 1
        assert files[0].filename == "test.pdf"
        # Verify base64 encoding
        decoded = base64.b64decode(files[0].bytes)
        assert decoded == b"PDF content here"

    @pytest.mark.asyncio
    async def test_resolve_missing_file(self, file_repository, s3_bucket, aws):
        import boto3
        from apis.shared.files.file_resolver import FileResolver
        s3 = boto3.client("s3", region_name="us-east-1")
        resolver = FileResolver(s3_client=s3)
        resolver._file_repository = file_repository
        files = await resolver.resolve_files("u1", ["nonexistent"])
        assert len(files) == 0

    @pytest.mark.asyncio
    async def test_resolve_max_files(self, file_repository, s3_bucket, aws):
        import boto3
        from apis.shared.files.file_resolver import FileResolver
        s3 = boto3.client("s3", region_name="us-east-1")
        for i in range(10):
            s3.put_object(Bucket=s3_bucket, Key=f"uploads/u1/f{i}", Body=b"x")
            await file_repository.create_file(_make_file(f"f{i}"))
        resolver = FileResolver(s3_client=s3)
        resolver._file_repository = file_repository
        files = await resolver.resolve_files("u1", [f"f{i}" for i in range(10)], max_files=3)
        assert len(files) == 3

    @pytest.mark.asyncio
    async def test_resolve_files_no_cap_when_max_files_is_none(self, file_repository, s3_bucket, aws):
        # The chat route applies the per-message cap itself (so it can tell
        # the user which files were left out) and passes None here.
        import boto3
        from apis.shared.files.file_resolver import FileResolver
        s3 = boto3.client("s3", region_name="us-east-1")
        for i in range(7):
            s3.put_object(Bucket=s3_bucket, Key=f"uploads/u1/f{i}", Body=b"x")
            await file_repository.create_file(_make_file(f"f{i}"))
        resolver = FileResolver(s3_client=s3)
        resolver._file_repository = file_repository
        files = await resolver.resolve_files("u1", [f"f{i}" for i in range(7)], max_files=None)
        assert len(files) == 7
