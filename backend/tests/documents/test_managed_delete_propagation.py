"""Deleting a document must remove it from the engine that actually serves it.

Before this, `cleanup_service` deleted the legacy S3 Vectors copy and the `DOC#`
row and never touched the managed knowledge base. On a promoted knowledge base
the content therefore stayed indexed forever:

* it kept being paid for, at $5.00/GB-month against S3 Vectors' ~$0.15;
* every orphan kept consuming a slot in `top_k`, because the status filter runs
  *after* retrieval — so a query could return five chunks and the model see two,
  with nothing logged and nothing raised;
* and the only thing preventing deleted content from being served was the
  fail-closed status filter, which has exactly one fail-open branch.

The most load-bearing assertion in this file is
`test_a_failed_managed_delete_keeps_the_document_record`: the `DOC#` row is what
the status filter joins against, so reporting success on a failed managed delete
would remove the row *and* leave the content — turning a storage leak into a
disclosure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from apis.app_api.documents.services import cleanup_service

ASSISTANT_ID = "ast-del-1"
DOCUMENT_ID = "DOC-del-1"


class RecordingManagedBackend:
    """Stands in for ManagedKbBackend, recording deletes and optionally failing."""

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.deleted: List[str] = []
        self.attempts = 0
        self._raises = raises

    async def delete_document(self, kb_ref: str, document_id: str) -> None:
        self.attempts += 1
        if self._raises is not None:
            raise self._raises
        self.deleted.append(document_id)


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> RecordingManagedBackend:
    backend = RecordingManagedBackend()
    import apis.shared.kb_backend.managed_backend as mb

    monkeypatch.setattr(mb, "ManagedKbBackend", lambda *a, **k: backend)
    return backend


def _set_engine(monkeypatch: pytest.MonkeyPatch, engine: Optional[str]) -> None:
    from apis.shared.kb_backend import records as r

    record: Optional[Dict[str, Any]]
    if engine is None:
        record = None
    else:
        record = {"retrievalEngine": engine} if engine != "absent" else {}

    monkeypatch.setattr(r, "get_kb_record", lambda *_: record)


def _raise_on_lookup(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    from apis.shared.kb_backend import records as r

    def _boom(*_: Any):
        raise exc

    monkeypatch.setattr(r, "get_kb_record", _boom)


async def _delete() -> bool:
    return await cleanup_service._delete_managed_documents_with_retries(
        ASSISTANT_ID, DOCUMENT_ID, max_retries=3, base_delay=0.0
    )


class TestAPromotedKnowledgeBaseHasTheDocumentRemoved:
    @pytest.mark.asyncio
    async def test_the_document_is_deleted_from_the_managed_kb(
        self, managed: RecordingManagedBackend, monkeypatch
    ):
        _set_engine(monkeypatch, "managed")

        assert await _delete() is True
        assert managed.deleted == [DOCUMENT_ID], (
            "a document deleted by its owner was left in the managed knowledge "
            "base; the corpus can only grow and every orphan costs a top_k slot"
        )


class TestALegacyKnowledgeBaseIsUntouched:
    """No managed copy to remove is success, not failure.

    Returning False here would block `hard_delete_document` for every legacy
    document in the system — the deletion would never complete.
    """

    @pytest.mark.asyncio
    async def test_an_absent_record_needs_no_managed_delete(
        self, managed: RecordingManagedBackend, monkeypatch
    ):
        _set_engine(monkeypatch, None)

        assert await _delete() is True
        assert managed.deleted == []

    @pytest.mark.asyncio
    async def test_a_record_with_no_engine_needs_no_managed_delete(
        self, managed: RecordingManagedBackend, monkeypatch
    ):
        """A migration in flight: `shadow`/`verify` have not promoted anything."""
        _set_engine(monkeypatch, "absent")

        assert await _delete() is True
        assert managed.deleted == []


class TestFailuresKeepTheDocumentRecordAlive:
    """The `DOC#` row is the safety net, so a failure must not report success."""

    @pytest.mark.asyncio
    async def test_a_failed_managed_delete_keeps_the_document_record(
        self, monkeypatch
    ):
        backend = RecordingManagedBackend(raises=RuntimeError("AccessDenied"))
        import apis.shared.kb_backend.managed_backend as mb

        monkeypatch.setattr(mb, "ManagedKbBackend", lambda *a, **k: backend)
        _set_engine(monkeypatch, "managed")

        assert await _delete() is False, (
            "a failed managed delete reported success; the caller would then "
            "hard-delete the DOC# row that the fail-closed status filter joins "
            "against, leaving indexed content with nothing left to hide it"
        )
        assert backend.attempts == 3, "every attempt should be retried"

    @pytest.mark.asyncio
    async def test_an_unreadable_record_fails_rather_than_assuming_legacy(
        self, managed: RecordingManagedBackend, monkeypatch
    ):
        """The opposite choice from the ingestion gate, on purpose.

        On ingest an unreadable record resolves to legacy, because the cost of
        being wrong is a duplicate index while the consumer still finishes the
        document. On delete the cost of being wrong is hard-deleting the row that
        keeps a still-indexed chunk unserved, so it fails and retries instead.
        """
        _raise_on_lookup(monkeypatch, RuntimeError("DynamoDB unavailable"))

        assert await _delete() is False
        assert managed.deleted == []

    @pytest.mark.asyncio
    async def test_a_promoted_kb_with_no_aws_ids_is_not_retried_forever(
        self, monkeypatch
    ):
        """Nothing was ever indexed, so there is nothing to remove."""
        from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

        backend = RecordingManagedBackend(raises=ManagedKbNotProvisioned("no awsKbId"))
        import apis.shared.kb_backend.managed_backend as mb

        monkeypatch.setattr(mb, "ManagedKbBackend", lambda *a, **k: backend)
        _set_engine(monkeypatch, "managed")

        assert await _delete() is True
        assert backend.attempts == 1, "not provisioned is terminal, not transient"


class TestTheCleanupContractIncludesManagedDeletion:
    @pytest.mark.asyncio
    async def test_a_failed_managed_delete_blocks_the_hard_delete(
        self, monkeypatch
    ):
        """End to end through `cleanup_document_resources`, not just the helper.

        The helper returning False is only useful if the caller conjoins it. This
        is the assertion that would fail if someone computed `all_succeeded`
        without the managed phase.
        """
        hard_deleted: List[str] = []

        async def _no_hard_delete(assistant_id: str, document_id: str) -> None:
            hard_deleted.append(document_id)

        async def _ok(*_: Any, **__: Any) -> bool:
            return True

        monkeypatch.setattr(cleanup_service, "_delete_vectors_with_retries", _ok)
        monkeypatch.setattr(cleanup_service, "_delete_s3_with_retries", _ok)

        async def _managed_fails(*_: Any, **__: Any) -> bool:
            return False

        monkeypatch.setattr(
            cleanup_service, "_delete_managed_documents_with_retries", _managed_fails
        )

        import apis.app_api.documents.services.document_service as ds

        monkeypatch.setattr(ds, "hard_delete_document", _no_hard_delete)

        result = await cleanup_service.cleanup_document_resources(
            document_id=DOCUMENT_ID,
            assistant_id=ASSISTANT_ID,
            s3_key=f"assistants/{ASSISTANT_ID}/documents/{DOCUMENT_ID}/f.pdf",
            chunk_count=3,
        )

        assert result is False
        assert hard_deleted == [], (
            "the DOC# row was hard-deleted even though the managed knowledge "
            "base still holds the content"
        )
