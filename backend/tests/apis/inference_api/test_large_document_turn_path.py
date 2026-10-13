"""Large documents ride a turn by reference, never by bytes.

``docs/specs/large-document-attachments.md`` §4B (PR-2). Pins:

- the resolver reads the row first and never fetches a reference-class object;
- ``_partition_attachments`` puts those files in their own ``reference`` list:
  counted in the marker, zero bytes against the inline budget, and the turn is
  classified as carrying a document (so ``document_read`` is injected);
- the user message gets the stored digest with its handle when preparation is
  ready, otherwise a fixed stub, and the text is a pure function of the row;
- the guidance sentence;
- direct ``files`` bytes over the inline limit stay oversized (§4B.6).
"""

from __future__ import annotations

import base64
import json
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

import apis.inference_api.chat.routes as routes
from apis.inference_api.chat.models import FileContent, InvocationRequest
from apis.shared.files import large_document
from apis.shared.files.document_digest import DigestSection, DocumentDigest
from apis.shared.files.file_resolver import FileResolver
from apis.shared.files.models import FileMetadata, FileStatus, INLINE_ATTACHMENTS_MAX_TOTAL_BYTES
from apis.shared.files.reference_attachments import (
    ReferenceAttachment,
    format_size,
    record_reference_attachments,
    render_reference,
    render_references_section,
    size_bucket,
)

MB = 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _meta(
    upload_id: str = "up-1",
    filename: str = "policy-manual.pdf",
    mime_type: str = PDF,
    size_bytes: int = 38 * MB,
    preparation: dict | None = None,
    digest: dict | None = None,
) -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id,
        user_id="user-1",
        session_id="sess-1",
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        s3_key=f"user-files/user-1/sess-1/{upload_id}/{filename}",
        s3_bucket="bucket",
        status=FileStatus.READY,
        preparation=preparation,
        digest=digest,
    )


def _ready_preparation(page_count: int = 312) -> dict:
    return large_document.preparation_record(
        status=large_document.STATUS_READY, fmt="pdf", page_count=page_count, shard_pages=10, text_bytes=900_000
    )


def _digest() -> dict:
    return DocumentDigest(
        format="pdf",
        unit="page",
        count=312,
        sections=[DigestSection(start=1, title="Purpose"), DigestSection(start=14, title="Leave & Absence")],
        abstract="The university's employment policies.",
        generated_at="2026-10-12T00:00:00+00:00",
    ).to_item()


# ---------------------------------------------------------------------------
# Resolver: metadata first
# ---------------------------------------------------------------------------


class TestResolverReadsTheRowFirst:
    def _resolver(self, rows: List[FileMetadata]) -> tuple[FileResolver, MagicMock]:
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"%PDF-1.7"))}
        resolver = FileResolver(s3_client=s3)
        by_id = {r.upload_id: r for r in rows}
        resolver._file_repository = MagicMock(get_file=AsyncMock(side_effect=lambda _u, uid: by_id.get(uid)))
        return resolver, s3

    @pytest.mark.asyncio
    async def test_a_reference_row_returns_metadata_and_never_fetches_the_object(self):
        row = _meta(preparation=large_document.preparation_record(status=large_document.STATUS_PENDING))
        resolver, s3 = self._resolver([row])

        [resolved] = await resolver.resolve_files("user-1", ["up-1"])

        s3.get_object.assert_not_called()
        assert resolved.reference is row
        assert resolved.bytes == ""
        assert (resolved.filename, resolved.content_type) == ("policy-manual.pdf", PDF)

    @pytest.mark.asyncio
    async def test_an_inline_class_row_is_fetched_and_encoded_as_before(self):
        row = _meta(size_bytes=1 * MB)
        resolver, s3 = self._resolver([row])

        [resolved] = await resolver.resolve_files("user-1", ["up-1"])

        s3.get_object.assert_called_once_with(Bucket="bucket", Key=row.s3_key)
        assert resolved.reference is None
        assert base64.b64decode(resolved.bytes) == b"%PDF-1.7"

    @pytest.mark.asyncio
    async def test_the_class_boundary_is_the_shared_predicate(self):
        # Exactly 4 MB is inline-class; one byte over is a reference. A large
        # spreadsheet is never a reference (it has its own tools).
        at_limit = _meta("a", size_bytes=4 * MB)
        over = _meta("b", size_bytes=4 * MB + 1)
        sheet = _meta("c", filename="big.xlsx", mime_type=XLSX, size_bytes=10 * MB)
        resolver, s3 = self._resolver([at_limit, over, sheet])

        resolved = await resolver.resolve_files("user-1", ["a", "b", "c"], max_files=None)

        assert [r.reference is not None for r in resolved] == [False, True, False]
        assert s3.get_object.call_count == 2


# ---------------------------------------------------------------------------
# Partition
# ---------------------------------------------------------------------------


def _b64_of_size(n: int) -> str:
    return base64.b64encode(b"x" * n).decode()


class TestReferencePartition:
    def test_reference_files_get_their_own_list(self):
        ref = ReferenceAttachment(meta=_meta())
        small = FileContent(filename="memo.pdf", content_type=PDF, bytes=_b64_of_size(1000))

        inline, tabular, presentations, oversized, reference = routes._partition_attachments([small, ref])

        assert inline == [small]
        assert reference == [ref]
        assert tabular == presentations == oversized == []

    def test_a_reference_adds_nothing_to_the_inline_byte_budget(self):
        ref = ReferenceAttachment(meta=_meta(size_bytes=49 * MB))
        inline_file = FileContent(filename="memo.pdf", content_type=PDF, bytes=_b64_of_size(3 * MB))

        inline, *_rest, reference = routes._partition_attachments([ref, inline_file])
        kept, over, requested = routes._apply_inline_byte_budget(inline, INLINE_ATTACHMENTS_MAX_TOTAL_BYTES)

        assert routes._estimate_decoded_size(ref) == 0
        assert requested == 3 * MB
        assert kept == [inline_file] and over == []

    def test_direct_bytes_over_the_inline_limit_stay_oversized(self):
        # §4B.6: bytes in the request body have no upload row to be a reference.
        direct = FileContent(filename="big.pdf", content_type=PDF, bytes=_b64_of_size(5 * MB))

        inline, _, _, oversized, reference = routes._partition_attachments([direct])

        assert oversized == [direct]
        assert inline == reference == []


class _FakeResolver:
    def __init__(self, resolved: list) -> None:
        self.resolved = resolved
        self.calls: List[dict] = []

    async def resolve_files(self, *, user_id: str, upload_ids: List[str], max_files: Any = None):
        self.calls.append({"upload_ids": list(upload_ids), "max_files": max_files})
        return self.resolved


@pytest.fixture
def phase(monkeypatch):
    state: dict = {"resolver": _FakeResolver([])}

    async def _no_pending(*args, **kwargs):
        return []

    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "pop_pending_attachments", _no_pending)
    monkeypatch.setattr(routes, "get_file_resolver", lambda: state["resolver"])

    async def run(upload_ids, files=None):
        req = InvocationRequest(session_id="sess-1", message="what does it say?", file_upload_ids=upload_ids, files=files)
        return await routes._resolve_turn_attachments(req, "user-1", None, is_resume=False, is_continuation=False)

    return run, state


class TestResolveTurnAttachments:
    @pytest.mark.asyncio
    async def test_a_reference_upload_stays_in_the_marker_and_injects_document_read(self, phase):
        from apis.shared.files.file_resolver import ResolvedFileContent

        run, state = phase
        row = _meta()
        state["resolver"] = _FakeResolver(
            [
                ResolvedFileContent(filename="photo.png", content_type="image/png", bytes=_b64_of_size(10)),
                ResolvedFileContent(filename=row.filename, content_type=row.mime_type, bytes="", reference=row),
            ]
        )

        out = await run(["up-0", "up-1"])

        assert [f.filename for f in out.files_to_send] == ["photo.png"]
        assert [r.upload_id for r in out.reference_documents] == ["up-1"]
        assert out.marker_names == ["photo.png", "policy-manual.pdf"]
        assert out.turn_has_document is True
        assert out.oversized_inline == [] and out.over_budget_inline == []

    @pytest.mark.asyncio
    async def test_references_count_toward_the_per_message_file_cap(self, phase, monkeypatch):
        # The cap is applied to the ids before resolution, so a reference uses
        # one of the five slots exactly like an inline file.
        run, state = phase
        monkeypatch.setattr(routes, "MAX_FILES_PER_MESSAGE", 2)

        out = await run(["up-1", "up-2", "up-3"])

        assert state["resolver"].calls[0]["upload_ids"] == ["up-1", "up-2"]
        assert out.dropped_over_count_total == 1

    @pytest.mark.asyncio
    async def test_a_reference_turn_records_the_content_free_metric(self, phase, monkeypatch):
        from apis.shared.files.file_resolver import ResolvedFileContent

        run, state = phase
        row = _meta()
        state["resolver"] = _FakeResolver(
            [ResolvedFileContent(filename=row.filename, content_type=row.mime_type, bytes="", reference=row)]
        )
        recorded: list = []
        monkeypatch.setattr(routes, "record_reference_attachments", lambda refs: recorded.append(list(refs)))

        await run(["up-1"])

        assert [[r.upload_id for r in refs] for refs in recorded] == [["up-1"]]


# ---------------------------------------------------------------------------
# What the user message carries
# ---------------------------------------------------------------------------


class TestDigestOrStub:
    def test_ready_renders_the_stored_digest_with_its_handle(self):
        ref = ReferenceAttachment(meta=_meta(preparation=_ready_preparation(), digest=_digest()))

        text = render_reference(ref, include_handle=True)

        assert text.startswith('<document-digest name="policy-manual.pdf" upload_id="up-1" format="pdf" pages="312">')
        assert "<abstract>The university's employment policies.</abstract>" in text
        assert '<section page="14">Leave &amp; Absence</section>' in text
        assert text.endswith("</document-digest>")

    def test_without_document_read_the_handle_is_omitted(self):
        ready = ReferenceAttachment(meta=_meta(preparation=_ready_preparation(), digest=_digest()))
        pending = ReferenceAttachment(meta=_meta(preparation={"status": "pending", "version": 1}))

        assert "upload_id=" not in render_reference(ready, include_handle=False)
        assert "upload_id=" not in render_reference(pending, include_handle=False)

    @pytest.mark.parametrize(
        "preparation,status",
        [
            (None, "preparing"),
            (large_document.preparation_record(status=large_document.STATUS_PENDING), "preparing"),
            (large_document.preparation_record(status=large_document.STATUS_FAILED, error="ValueError"), "failed"),
            # Prepared, but the digest was not built (DOCUMENT_DIGEST_ENABLED=false).
            (_ready_preparation(), "ready"),
        ],
    )
    def test_anything_short_of_a_ready_digest_is_the_fixed_stub(self, preparation, status):
        ref = ReferenceAttachment(meta=_meta(preparation=preparation))

        assert render_reference(ref, include_handle=True) == (
            f'<document-reference name="policy-manual.pdf" upload_id="up-1" '
            f'size_bytes="{38 * MB}" status="{status}"/>'
        )

    def test_a_failed_digest_on_a_ready_row_is_the_stub(self):
        failed = {**_digest(), "status": "failed"}
        ref = ReferenceAttachment(meta=_meta(preparation=_ready_preparation(), digest=failed))

        assert render_reference(ref, include_handle=True).startswith("<document-reference ")

    def test_the_stub_escapes_the_filename(self):
        ref = ReferenceAttachment(meta=_meta(filename='Q3 "final" <draft> & notes.pdf'))

        assert 'name="Q3 &quot;final&quot; &lt;draft&gt; &amp; notes.pdf"' in render_reference(
            ref, include_handle=True
        )

    def test_the_section_is_byte_stable_and_in_attachment_order(self):
        first = _meta("up-1", preparation=_ready_preparation(), digest=_digest())
        second = _meta("up-2", filename="handbook.docx", mime_type=DOCX, size_bytes=12 * MB)

        a = render_references_section(
            [ReferenceAttachment(meta=first), ReferenceAttachment(meta=second)], include_handle=True
        )
        b = render_references_section(
            [ReferenceAttachment(meta=first.model_copy()), ReferenceAttachment(meta=second.model_copy())],
            include_handle=True,
        )

        assert a == b
        assert a.index('upload_id="up-1"') < a.index('upload_id="up-2"')
        assert render_references_section([], include_handle=True) == ""


class TestGuidance:
    def test_a_pdf_points_at_pattern_then_page_range(self):
        ref = ReferenceAttachment(meta=_meta())

        text = routes._build_attachment_guidance([], [], [], None, references=[ref])

        assert text == (
            "_`policy-manual.pdf` (38 MB) is attached but too large to include in full, so "
            "its content is not in this message. To answer from it, call `document_read` "
            "with `upload_id` `up-1`: first with `pattern` to find the relevant pages, then "
            "with `page_range` to read them._"
        )

    def test_a_non_pdf_has_no_pages_to_range_over(self):
        ref = ReferenceAttachment(meta=_meta(filename="handbook.docx", mime_type=DOCX, size_bytes=int(12.4 * MB)))

        text = routes._build_attachment_guidance([], [], [], None, references=[ref])

        assert "`handbook.docx` (12.4 MB)" in text
        assert "`pattern`" in text and "page_range" not in text

    def test_without_document_read_the_tool_is_not_promised(self):
        ref = ReferenceAttachment(meta=_meta())

        text = routes._build_attachment_guidance([], [], [], None, references=[ref], document_read_available=False)

        assert "document_read" not in text and "upload_id" not in text
        assert "too large to include in full" in text

    def test_no_references_no_sentence(self):
        assert routes._build_attachment_guidance([], [], [], None) == ""


class TestMetric:
    def test_size_buckets_are_coarse(self):
        assert [size_bucket(n) for n in (5 * MB, 8 * MB, 9 * MB, 20 * MB, 50 * MB)] == [
            "4-8MB", "4-8MB", "8-16MB", "16-32MB", "32-50MB",
        ]
        assert format_size(38 * MB) == "38 MB"

    def test_one_content_free_record_per_reference(self, monkeypatch, capsys):
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)
        emitted: list = []
        import apis.shared.observability.emf as emf

        monkeypatch.setattr(emf, "emit_emf_metrics", lambda ns, **kw: emitted.append((ns, kw)))

        record_reference_attachments([ReferenceAttachment(meta=_meta(preparation=_ready_preparation()))])

        [(namespace, record)] = emitted
        assert namespace == "AgentCoreStack/Compaction"
        assert record["metrics"] == {"AttachmentReference": 1}
        assert record["properties"] == {"sizeBucket": "32-50MB", "format": "pdf", "status": "ready"}
        assert "policy-manual" not in json.dumps(record)
