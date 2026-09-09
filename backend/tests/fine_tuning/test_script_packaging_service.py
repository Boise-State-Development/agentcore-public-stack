"""Unit tests for ScriptPackagingService."""

import io
import tarfile

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

from apis.app_api.fine_tuning import task_types
from apis.app_api.fine_tuning.script_packaging_service import (
    ScriptPackagingService,
    scripts_s3_key,
)

TEXT = task_types.TEXT_CLASSIFICATION
VLM = task_types.IMAGE_TEXT_TO_TEXT

TEXT_KEY = scripts_s3_key(task_types.DLC_FAMILY_TEXT)
VLM_KEY = scripts_s3_key(task_types.DLC_FAMILY_VLM)


@pytest.fixture
def mock_s3():
    return MagicMock()


@pytest.fixture
def service(mock_s3):
    return ScriptPackagingService(s3_client=mock_s3, bucket_name="test-bucket")


def _archive_member(tar_bytes, name):
    """Return one member's bytes from an in-memory tar.gz."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        return tar.extractfile(name).read().decode("utf-8")


class TestEnsureScriptsUploaded:

    def test_uploads_when_not_in_s3(self, service, mock_s3):
        """Should upload tar.gz when S3 object doesn't exist (404)."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        result = service.ensure_scripts_uploaded(TEXT)

        assert result == f"s3://test-bucket/{TEXT_KEY}"
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == TEXT_KEY
        assert "content-hash" in call_kwargs["Metadata"]

    def test_skips_upload_when_hash_matches(self, service, mock_s3):
        """Should skip upload when S3 object has matching content hash."""
        content_hash = service._compute_content_hash(task_types.DLC_FAMILY_TEXT)
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": content_hash},
        }

        result = service.ensure_scripts_uploaded(TEXT)

        assert result == f"s3://test-bucket/{TEXT_KEY}"
        mock_s3.put_object.assert_not_called()

    def test_reuploads_when_hash_differs(self, service, mock_s3):
        """Should re-upload when S3 object has a different content hash."""
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": "stale-hash-from-old-scripts"},
        }

        result = service.ensure_scripts_uploaded(TEXT)

        assert result == f"s3://test-bucket/{TEXT_KEY}"
        mock_s3.put_object.assert_called_once()

    def test_caches_uri_after_first_call(self, service, mock_s3):
        """Should cache URI and skip S3 checks on subsequent calls."""
        content_hash = service._compute_content_hash(task_types.DLC_FAMILY_TEXT)
        mock_s3.head_object.return_value = {
            "Metadata": {"content-hash": content_hash},
        }

        uri1 = service.ensure_scripts_uploaded(TEXT)
        uri2 = service.ensure_scripts_uploaded(TEXT)

        assert uri1 == uri2
        # head_object should only be called once (cached after first call)
        mock_s3.head_object.assert_called_once()

    def test_omitting_the_task_uses_the_legacy_default(self, service, mock_s3):
        """A caller that passes nothing gets the text family, as before."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        assert service.ensure_scripts_uploaded() == f"s3://test-bucket/{TEXT_KEY}"

    def test_sets_metadata_on_upload(self, service, mock_s3):
        """Should include content-hash metadata when uploading."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        service.ensure_scripts_uploaded(TEXT)

        call_kwargs = mock_s3.put_object.call_args[1]
        metadata = call_kwargs["Metadata"]
        assert "content-hash" in metadata
        assert len(metadata["content-hash"]) == 64  # SHA256 hex digest


class TestPerFamilyPackaging:
    """One archive per DLC family, because the dependency sets differ."""

    def test_families_use_separate_keys(self, service, mock_s3):
        """A shared key would let the last job overwrite another family's deps."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        assert service.ensure_scripts_uploaded(TEXT) == f"s3://test-bucket/{TEXT_KEY}"
        assert service.ensure_scripts_uploaded(VLM) == f"s3://test-bucket/{VLM_KEY}"
        assert TEXT_KEY != VLM_KEY

    def test_cache_is_per_family(self, service, mock_s3):
        """A cached text URI must not be served to a VLM job."""
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        service.ensure_scripts_uploaded(TEXT)
        service.ensure_scripts_uploaded(VLM)

        keys = [c[1]["Key"] for c in mock_s3.put_object.call_args_list]
        assert keys == [TEXT_KEY, VLM_KEY]

    def test_family_hashes_differ(self, service):
        """Same scripts, different requirements — the hash has to notice."""
        assert service._compute_content_hash(
            task_types.DLC_FAMILY_TEXT
        ) != service._compute_content_hash(task_types.DLC_FAMILY_VLM)

    def test_vlm_archive_carries_the_peft_stack(self, service):
        """peft and bitsandbytes must reach the container that needs them."""
        tar_bytes = service._create_tar_gz(task_types.DLC_FAMILY_VLM)
        requirements = _archive_member(tar_bytes, "requirements.txt")
        assert "peft==" in requirements
        assert "bitsandbytes==" in requirements

    def test_text_archive_excludes_bitsandbytes(self, service):
        """bitsandbytes needs torch>=2.4; the text DLC is torch 2.1.

        Shipping it to the text container would break dependency installation
        for every existing text job.
        """
        tar_bytes = service._create_tar_gz(task_types.DLC_FAMILY_TEXT)
        requirements = _archive_member(tar_bytes, "requirements.txt")
        assert "bitsandbytes" not in requirements
        assert "peft" not in requirements

    def test_every_family_packages_a_requirements_file(self, service):
        """A family with no requirements.txt installs nothing and fails late."""
        for family in (
            task_types.DLC_FAMILY_TEXT,
            task_types.DLC_FAMILY_VISION,
            task_types.DLC_FAMILY_VLM,
        ):
            names = [n for n, _ in service._source_paths(family)]
            assert names.count("requirements.txt") == 1

    def test_every_task_module_is_packaged(self, service):
        """train.py imports every task module at load time."""
        names = [n for n, _ in service._source_paths(task_types.DLC_FAMILY_VLM)]
        assert "task_image_text_to_text.py" in names
        assert "task_types.py" in names


class TestComputeContentHash:

    def test_returns_consistent_hash(self, service):
        """Same scripts should produce the same hash."""
        hash1 = service._compute_content_hash(task_types.DLC_FAMILY_TEXT)
        hash2 = service._compute_content_hash(task_types.DLC_FAMILY_TEXT)
        assert hash1 == hash2

    def test_hash_is_64_char_hex(self, service):
        """SHA256 hex digest should be 64 characters."""
        content_hash = service._compute_content_hash(task_types.DLC_FAMILY_TEXT)
        assert len(content_hash) == 64
        assert all(c in "0123456789abcdef" for c in content_hash)


class TestCreateTarGz:

    def test_produces_non_empty_bytes(self, service):
        """Should create a non-empty tar.gz archive."""
        tar_bytes = service._create_tar_gz(task_types.DLC_FAMILY_TEXT)
        assert len(tar_bytes) > 0

    def test_is_valid_gzip(self, service):
        """Output should start with gzip magic bytes."""
        tar_bytes = service._create_tar_gz(task_types.DLC_FAMILY_TEXT)
        # Gzip magic bytes: 0x1f 0x8b
        assert tar_bytes[0:2] == b"\x1f\x8b"
