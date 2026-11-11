"""An uploaded document over the combined inline budget goes by reference.

The planner (``attachment_plan``, ``over_budget_reference``) decides which file
does not fit and whether it can go by reference; those cases are rows of its
table (``tests/apis/shared/files/test_attachment_plan.py``). This file pins the
route's side of it: a planned ``on_demand`` file becomes a
``ReferenceAttachment(over_budget=True)`` built from the row the resolver
already read, placed among the turn's other references in attachment order,
and kept in the marker; its stored in-process digest (or a ``status="ready"``
stub) is in the user message and ``document_read`` reads the original. Pins:

- the shape that prompted it, end to end through ``_resolve_turn_attachments``;
- a turn within the budget is unchanged (kept set, references, marker, text);
- the rendered text is a pure function of the row (prompt-cache contract);
- the guidance sentence names the combined limit and never offers ``query``;
- ``INLINE_BUDGET_BY_REFERENCE_ENABLED=false`` restores the drop;
- the SPA's mirrored constants match the server's.
"""

from __future__ import annotations

import base64
from typing import Any, List

import pytest

import apis.inference_api.chat.routes as routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.feature_flags import inline_budget_by_reference_enabled
from apis.shared.files.attachment_plan import delivers_by_reference
from apis.shared.files.document_digest import DigestSection, DocumentDigest
from apis.shared.files.file_resolver import FileResolver, ResolvedFileContent
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.files.reference_attachments import (
    ReferenceAttachment,
    render_reference,
    render_references_section,
    size_bucket,
)

MB = 1_000_000
MIB = 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _b64_of_size(n: int) -> str:
    return base64.b64encode(b"\x00" * n).decode("ascii")


def _digest() -> dict:
    return DocumentDigest(
        format="pdf",
        unit="page",
        count=48,
        sections=[DigestSection(start=1, title="Scope"), DigestSection(start=9, title="Export Controls")],
        abstract="A peer institution's research security policy.",
        generated_at="2026-11-01T00:00:00+00:00",
    ).to_item()


def _row(upload_id: str, filename: str, size: int, mime_type: str = PDF, digest: dict | None = None) -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id,
        user_id="user-1",
        session_id="sess-1",
        filename=filename,
        mime_type=mime_type,
        size_bytes=size,
        s3_key=f"user-files/user-1/sess-1/{upload_id}/{filename}",
        s3_bucket="bucket",
        status=FileStatus.READY,
        digest=digest,
    )


def _resolved(row: FileMetadata) -> ResolvedFileContent:
    """What the resolver returns for ``row``: the row alone for a reference-class
    upload, its bytes and its row for an inline-class one."""
    if delivers_by_reference(row):
        return ResolvedFileContent(row.filename, row.mime_type, "", reference=row, meta=row)
    return ResolvedFileContent(row.filename, row.mime_type, _b64_of_size(row.size_bytes), meta=row)


class _FakeResolver:
    def __init__(self, resolved: list) -> None:
        self.resolved = resolved

    async def resolve_files(self, *, user_id: str, upload_ids: List[str], max_files: Any = None):
        return self.resolved


@pytest.fixture
def phase(monkeypatch):
    state: dict = {"resolver": _FakeResolver([])}

    async def _no_pending(*args, **kwargs):
        return []

    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "pop_pending_attachments", _no_pending)
    monkeypatch.setattr(routes, "get_file_resolver", lambda: state["resolver"])
    monkeypatch.setattr(routes, "record_reference_attachments", lambda refs: None)
    monkeypatch.delenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", raising=False)

    async def run(rows: list, files: list | None = None):
        state["resolver"] = _FakeResolver([_resolved(r) for r in rows])
        req = InvocationRequest(
            session_id="sess-1",
            message="compare these policies",
            file_upload_ids=[r.upload_id for r in rows] or None,
            files=files,
        )
        return await routes._resolve_turn_attachments(req, "user-1", None, is_resume=False, is_continuation=False)

    return run


def _incident_rows() -> list:
    # Five policy PDFs, in the order attached. The first is over the per-file
    # inline limit (reference-class); the other four each pass it, but the
    # third inline candidate pushes the running total past the 7.0 MB budget.
    return [
        _row("up-a", "policy-a.pdf", int(5.7 * MB)),
        _row("up-b", "policy-b.pdf", int(3.9 * MB)),
        _row("up-c", "policy-c.pdf", int(0.17 * MB)),
        _row("up-d", "policy-d.pdf", int(4.0 * MB), digest=_digest()),
        _row("up-e", "policy-e.pdf", int(2.7 * MB)),
    ]


def _message_text(out, *, document_read_available: bool = True) -> str:
    """The parts of the persisted user message this change can move."""
    guidance = routes._build_attachment_guidance(
        out.diverted_tabular,
        out.diverted_presentations,
        out.oversized_inline,
        [],
        over_budget=out.over_budget_inline,
        references=out.reference_documents,
        document_read_available=document_read_available,
    )
    section = render_references_section(out.reference_documents, include_handle=document_read_available)
    return f"{guidance}\n\n{section}\n\n{out.marker_names}"


class TestOverBudgetDocumentGoesByReference:
    @pytest.mark.asyncio
    async def test_the_incident_shape_keeps_every_file(self, phase):
        out = await phase(_incident_rows())

        assert [f.filename for f in out.files_to_send] == ["policy-b.pdf", "policy-c.pdf", "policy-e.pdf"]
        # Attachment order across both kinds of reference.
        assert [r.upload_id for r in out.reference_documents] == ["up-a", "up-d"]
        assert [r.over_budget for r in out.reference_documents] == [False, True]
        assert out.over_budget_inline == []
        # Nothing was dropped, so every file keeps its card on reload.
        assert out.marker_names == [f"policy-{x}.pdf" for x in "abcde"]
        assert out.turn_has_document is True
        # The cost row records the decision behind it.
        assert [(e["mode"], e["reason"]) for e in out.plan.to_row()["files"]] == [
            ("on_demand", "reference_class"),
            ("native", "inline"),
            ("native", "inline"),
            ("on_demand", "over_budget_reference"),
            ("native", "inline"),
        ]

    @pytest.mark.asyncio
    async def test_the_guidance_names_the_combined_limit_and_document_read(self, phase):
        out = await phase(_incident_rows())

        text = routes._build_attachment_guidance(
            [], [], [], [], over_budget=out.over_budget_inline, references=out.reference_documents,
            document_read_available=True, search_available=True,
        )

        assert "were skipped" not in text
        assert (
            "_`policy-d.pdf` (3.8 MB) is attached, but this message's attachments together exceed the "
            "combined size limit for a single message, so its content is not in this message. To answer "
            "from it, call `document_read` with `upload_id` `up-d`: first with `pattern` to find the "
            "relevant pages, then with `page_range` to read them._"
        ) in text
        # The large document still gets search; the over-budget one never does,
        # because only large documents are indexed.
        assert text.count("`query`") == 1

    @pytest.mark.asyncio
    async def test_without_document_read_the_note_says_so(self, phase):
        out = await phase(_incident_rows())

        text = routes._build_attachment_guidance(
            [], [], [], [], references=out.reference_documents[1:], document_read_available=False
        )

        assert text.endswith("Reading parts of it is not available in this conversation._")
        assert "upload_id" not in render_references_section(out.reference_documents, include_handle=False)

    @pytest.mark.asyncio
    async def test_the_message_text_is_byte_stable(self, phase):
        # Same rows, same text: the persisted user message lands in the
        # cacheable prefix on every later turn.
        first = _message_text(await phase(_incident_rows()))
        second = _message_text(await phase(_incident_rows()))

        assert first == second
        assert '<document-digest name="policy-d.pdf" upload_id="up-d"' in first

    @pytest.mark.asyncio
    async def test_the_kill_switch_restores_the_drop(self, phase, monkeypatch):
        monkeypatch.setenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", "false")

        out = await phase(_incident_rows())

        assert [f.filename for f in out.over_budget_inline] == ["policy-d.pdf"]
        assert [r.upload_id for r in out.reference_documents] == ["up-a"]
        assert "policy-d.pdf" not in out.marker_names


class TestWithinTheBudgetNothingChanges:
    @pytest.mark.asyncio
    async def test_kept_set_references_marker_and_text_match_the_kill_switch(self, phase, monkeypatch):
        rows = [
            _row("up-a", "policy-a.pdf", int(5.7 * MB)),
            _row("up-b", "policy-b.pdf", int(3.9 * MB)),
            _row("up-c", "policy-c.pdf", int(2.7 * MB)),
            _row("up-d", "chart.png", int(0.3 * MB), mime_type="image/png"),
        ]

        on = await phase(rows)
        monkeypatch.setenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", "false")
        off = await phase(rows)

        assert [f.filename for f in on.files_to_send] == ["policy-b.pdf", "policy-c.pdf", "chart.png"]
        assert [f.filename for f in on.files_to_send] == [f.filename for f in off.files_to_send]
        assert [f.bytes for f in on.files_to_send] == [f.bytes for f in off.files_to_send]
        assert [r.upload_id for r in on.reference_documents] == ["up-a"] == [
            r.upload_id for r in off.reference_documents
        ]
        assert on.over_budget_inline == off.over_budget_inline == []
        assert on.marker_names == off.marker_names
        assert _message_text(on) == _message_text(off)


class TestRendering:
    def test_an_over_budget_reference_renders_its_in_process_digest(self):
        # Never prepared: the digest was built in-process at upload.
        ref = ReferenceAttachment(meta=_row("up-d", "policy-d.pdf", 4 * MB, digest=_digest()), over_budget=True)

        text = render_reference(ref, include_handle=True)

        assert text.startswith('<document-digest name="policy-d.pdf" upload_id="up-d" format="pdf" pages="48">')

    def test_without_a_digest_the_stub_says_ready_not_preparing(self):
        # `document_read` reads a file this size from the original right away.
        ref = ReferenceAttachment(meta=_row("up-d", "policy-d.pdf", 4 * MB), over_budget=True)

        assert render_reference(ref, include_handle=True) == (
            f'<document-reference name="policy-d.pdf" upload_id="up-d" size_bytes="{4 * MB}" status="ready"/>'
        )

    def test_a_large_reference_is_unchanged(self):
        # The same row read as an ordinary reference is never prepared, so it
        # still renders "preparing" (the default ``over_budget=False``).
        ref = ReferenceAttachment(meta=_row("up-a", "policy-a.pdf", int(5.7 * MB), digest=_digest()))

        assert 'status="preparing"' in render_reference(ref, include_handle=True)

    def test_size_bucket_has_a_bucket_under_the_inline_limit(self):
        assert size_bucket(int(3.9 * MB)) == "0-4MB"
        assert size_bucket(5 * MIB) == "4-8MB"


class TestResolverCarriesTheRow:
    @pytest.mark.asyncio
    async def test_an_inline_class_row_comes_back_with_its_bytes_and_its_row(self):
        from unittest.mock import AsyncMock, MagicMock

        row = _row("up-1", "policy.pdf", 1 * MB)
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"%PDF-1.7"))}
        resolver = FileResolver(s3_client=s3)
        resolver._file_repository = MagicMock(get_file=AsyncMock(return_value=row))

        [resolved] = await resolver.resolve_files("user-1", ["up-1"])

        assert resolved.meta is row
        assert resolved.reference is None
        assert base64.b64decode(resolved.bytes) == b"%PDF-1.7"


class TestKillSwitch:
    @pytest.mark.parametrize("value", [None, "", "true", "TRUE", "1"])
    def test_default_on(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", raising=False)
        else:
            monkeypatch.setenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", value)
        assert inline_budget_by_reference_enabled() is True

    @pytest.mark.parametrize("value", ["false", "FALSE", " false "])
    def test_only_literal_false_disables(self, monkeypatch, value):
        monkeypatch.setenv("INLINE_BUDGET_BY_REFERENCE_ENABLED", value)
        assert inline_budget_by_reference_enabled() is False


# The SPA reads these limits from GET /files/limits; its one built-in fallback
# is pinned against the endpoint in tests/apis/app_api/test_file_limits_route.py.
