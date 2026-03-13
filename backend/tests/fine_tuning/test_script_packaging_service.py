"""Unit tests for ScriptPackagingService."""

import pytest
from unittest.mock import MagicMock, patch, mock_open
from botocore.exceptions import ClientError

from apis.app_api.fine_tuning.script_packaging_service import (
    ScriptPackagingService,
    SCRIPTS_S3_KEY,
)


@pytest.fixture
def mock_s3():
    return MagicMock()


@pytest.fixture
def service(mock_s3):
    return ScriptPackagingService(s3_client=mock_s3, bucket_name="test-bucket")


class TestEnsureScriptsUploaded:

    def test_uploads_when_not_in_s3(self, service, mock_s3):
        """Should upload tar.gz when S3 object doesn't exist (404)."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        result = service.ensure_scripts_uploaded()

        assert result == f"s3://test-bucket/{SCRIPTS_S3_KEY}"
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == SCRIPTS_S3_KEY
        assert "content-hash" in call_kwargs["Metadata"]

    def test_skips_upload_when_hash_matches(self, service, mock_s3):
        """Should skip upload when S3 object has matching content hash."""
        content_hash = service._compute_content_hash()
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": content_hash},
        }

        result = service.ensure_scripts_uploaded()

        assert result == f"s3://test-bucket/{SCRIPTS_S3_KEY}"
        mock_s3.put_object.assert_not_called()

    def test_reuploads_when_hash_differs(self, service, mock_s3):
        """Should re-upload when S3 object has a different content hash."""
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": "stale-hash-from-old-scripts"},
        }

        result = service.ensure_scripts_uploaded()

        assert result == f"s3://test-bucket/{SCRIPTS_S3_KEY}"
        mock_s3.put_object.assert_called_once()

    def test_caches_uri_after_first_call(self, service, mock_s3):
        """Should cache URI and skip S3 checks on subsequent calls."""
        content_hash = service._compute_content_hash()
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": content_hash},
        }

        uri1 = service.ensure_scripts_uploaded()
        uri2 = service.ensure_scripts_uploaded()

        assert uri1 == uri2
        # head_object should only be called once (cached after first call)
        mock_s3.head_object.assert_called_once()

    def test_sets_metadata_on_upload(self, service, mock_s3):
        """Should include content-hash metadata when uploading."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        service.ensure_scripts_uploaded()

        call_kwargs = mock_s3.put_object.call_args[1]
        metadata = call_kwargs["Metadata"]
        assert "content-hash" in metadata
        assert len(metadata["content-hash"]) == 64  # SHA256 hex digest


class TestComputeContentHash:

    def test_returns_consistent_hash(self, service):
        """Same scripts should produce the same hash."""
        hash1 = service._compute_content_hash()
        hash2 = service._compute_content_hash()
        assert hash1 == hash2

    def test_hash_is_64_char_hex(self, service):
        """SHA256 hex digest should be 64 characters."""
        content_hash = service._compute_content_hash()
        assert len(content_hash) == 64
        assert all(c in "0123456789abcdef" for c in content_hash)


class TestCreateTarGz:

    def test_produces_non_empty_bytes(self, service):
        """Should create a non-empty tar.gz archive."""
        tar_bytes = service._create_tar_gz()
        assert len(tar_bytes) > 0

    def test_is_valid_gzip(self, service):
        """Output should start with gzip magic bytes."""
        tar_bytes = service._create_tar_gz()
        # Gzip magic bytes: 0x1f 0x8b
        assert tar_bytes[0:2] == b"\x1f\x8b"
