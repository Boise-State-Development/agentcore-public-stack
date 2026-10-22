"""Folder attachment rows, the ``delivery`` field and the folder flag
(``docs/specs/drive-folder-attachments.md`` §4.2, §4.3, §4.8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from apis.shared.feature_flags import folder_attachments_enabled
from apis.shared.files import folder_attachments as fa
from apis.shared.files.folder_attachments import (
    FolderAttachment,
    FolderCounts,
    FolderFileEntry,
    new_folder_attach_id,
    record_folder_expansion,
)
from apis.shared.files.models import (
    DELIVERY_INLINE,
    DELIVERY_REFERENCE,
    INLINE_DOCUMENT_MAX_BYTES,
    FileMetadata,
    FileStatus,
    is_reference_document,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _folder(**overrides) -> FolderAttachment:
    fields = dict(
        folder_attach_id="fa-1",
        session_id="sess-1",
        user_id="alice",
        connector_id="google",
        provider_folder_id="drive-folder-1",
        folder_name="BIO 101",
        folder_path="My Drive/BIO 101",
    )
    fields.update(overrides)
    return FolderAttachment(**fields)


def _meta(filename="a.pdf", mime="application/pdf", size=1000, delivery=DELIVERY_INLINE) -> FileMetadata:
    return FileMetadata(
        upload_id="up-1", user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/u1/s1/up-1/{filename}", s3_bucket="b",
        status=FileStatus.READY, delivery=delivery,
    )


# ---------------------------------------------------------------------------
# delivery on file rows (§4.3)
# ---------------------------------------------------------------------------


class TestDelivery:
    def test_defaults_to_inline_and_is_not_written(self):
        item = _meta().to_dynamo_item()
        assert "delivery" not in item
        assert FileMetadata.from_dynamo_item(item).delivery == DELIVERY_INLINE

    def test_reference_round_trips(self):
        item = _meta(delivery=DELIVERY_REFERENCE).to_dynamo_item()
        assert item["delivery"] == "reference"
        assert FileMetadata.from_dynamo_item(item).delivery == DELIVERY_REFERENCE

    def test_an_old_row_without_the_field_reads_as_inline(self):
        item = _meta().to_dynamo_item()
        item.pop("delivery", None)
        assert FileMetadata.from_dynamo_item(item).delivery == DELIVERY_INLINE

    def test_an_unknown_value_reads_as_inline(self):
        item = _meta().to_dynamo_item()
        item["delivery"] = "sideways"
        assert FileMetadata.from_dynamo_item(item).delivery == DELIVERY_INLINE

    @pytest.mark.parametrize("filename,mime", [("a.pdf", "application/pdf"), ("a.docx", DOCX), ("a.md", "text/markdown")])
    def test_reference_delivery_makes_a_small_document_reference_class(self, filename, mime):
        assert not is_reference_document(_meta(filename, mime, size=300 * 1024))
        assert is_reference_document(_meta(filename, mime, size=300 * 1024, delivery=DELIVERY_REFERENCE))

    def test_the_size_rule_still_applies_to_inline_delivery(self):
        assert is_reference_document(_meta(size=INLINE_DOCUMENT_MAX_BYTES + 1))

    @pytest.mark.parametrize("filename,mime", [("a.xlsx", XLSX), ("a.csv", "text/csv"), ("a.png", "image/png")])
    def test_reference_delivery_never_promotes_other_classes(self, filename, mime):
        assert not is_reference_document(_meta(filename, mime, size=300 * 1024, delivery=DELIVERY_REFERENCE))


# ---------------------------------------------------------------------------
# The folder row (§4.2)
# ---------------------------------------------------------------------------


class TestFolderRow:
    def test_keys_and_ttl(self):
        item = _folder().to_dynamo_item()
        assert item["PK"] == "USER#alice"
        assert item["SK"] == "FOLDER#fa-1"
        assert item["GSI1PK"] == "CONV#sess-1"
        assert item["GSI1SK"] == "FOLDER#fa-1"
        assert item["status"] == "expanding"
        assert item["counts"] == {"listed": 0, "attached": 0, "skippedType": 0, "skippedSize": 0, "failed": 0, "truncated": False}
        assert item["ttl"] > int(datetime.now(timezone.utc).timestamp()) + 364 * 24 * 3600

    def test_round_trips_through_dynamo_decimals(self):
        folder = _folder(
            status="partial",
            error="quota_exceeded",
            counts=FolderCounts(listed=3, attached=1, skipped_type=1, failed=1, truncated=True),
            files=[
                FolderFileEntry(provider_file_id="f1", name="syllabus.pdf", relative_path="syllabus.pdf",
                                mime_type="application/pdf", size_bytes=1200, upload_id="up-1", status="attached"),
                FolderFileEntry(provider_file_id="f2", name="late.docx", relative_path="week 1/late.docx",
                                mime_type=DOCX, status="failed", error="download failed"),
            ],
            snapshot_at="2026-10-13T10:00:00Z",
        )
        item = folder.to_dynamo_item()
        # What DynamoDB hands back: numbers as Decimal, keys included.
        item["counts"] = {k: (Decimal(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in item["counts"].items()}
        item["files"][0]["sizeBytes"] = Decimal(1200)
        back = FolderAttachment.from_dynamo_item(item)
        assert back.model_dump(exclude={"ttl"}) == folder.model_dump(exclude={"ttl"})
        assert back.ttl == item["ttl"]
        assert back.counts.listed == 3 and isinstance(back.counts.listed, int)
        assert back.files[0].size_bytes == 1200
        assert back.attached_provider_ids == {"f1"}

    def test_stale_only_when_expanding_and_quiet_past_the_timeout(self):
        now = datetime.now(timezone.utc)
        fresh = _folder(updated_at=(now - timedelta(minutes=1)).isoformat())
        old = _folder(updated_at=(now - timedelta(minutes=16)).isoformat())
        assert not fresh.is_stale(now)
        assert old.is_stale(now)
        assert not _folder(status="ready", updated_at=old.updated_at).is_stale(now)
        assert old.is_stale(now, timeout_seconds=20 * 60) is False

    def test_ids_sort_by_time(self):
        a, b = new_folder_attach_id(), new_folder_attach_id()
        assert a != b
        assert a.split("_")[0] <= b.split("_")[0]

    def test_caps_default_per_spec(self):
        assert fa.FOLDER_ATTACH_MAX_FILES == 100
        assert fa.FOLDER_ATTACH_MAX_BYTES == 250 * 1024 * 1024
        assert fa.FOLDER_ATTACH_CONCURRENCY == 3
        assert fa.FOLDER_EXPANSION_TIMEOUT == 15 * 60


# ---------------------------------------------------------------------------
# The metric (§6) and the flag (§4.8)
# ---------------------------------------------------------------------------


class TestMetric:
    def test_emits_counts_only(self, monkeypatch):
        emitted = []
        monkeypatch.setattr("apis.shared.observability.emf.emit_emf_metrics", lambda *a, **k: emitted.append((a, k)))
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", lambda: True)
        counts = FolderCounts(listed=5, attached=3, skipped_type=1, skipped_size=0, failed=1, truncated=True)

        record_folder_expansion(outcome="partial", counts=counts, bytes_attached=4096, ms=1500)

        (namespace,), kwargs = emitted[0]
        assert namespace == "AgentCoreStack/Compaction"
        assert kwargs["metrics"]["FolderAttachment"] == 1
        assert kwargs["metrics"]["FolderFilesAttached"] == 3
        assert kwargs["metrics"]["FolderBytesAttached"] == 4096
        assert kwargs["properties"] == {"outcome": "partial", "truncated": "true"}
        # Content-free: no name, path, id or user anywhere in the record.
        assert "BIO" not in str(emitted) and "alice" not in str(emitted)

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", MagicMock(side_effect=RuntimeError("boom")))
        record_folder_expansion(outcome="ready", counts=FolderCounts(), bytes_attached=0, ms=0)


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_only_false_disables(monkeypatch, value):
    monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", value)
    assert not folder_attachments_enabled()


@pytest.mark.parametrize("value", ["", "true", "yes", "1", "off"])
def test_anything_else_is_on(monkeypatch, value):
    monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", value)
    assert folder_attachments_enabled()


def test_unset_is_on(monkeypatch):
    monkeypatch.delenv("FOLDER_ATTACHMENTS_ENABLED", raising=False)
    assert folder_attachments_enabled()
