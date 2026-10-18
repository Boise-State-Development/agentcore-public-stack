"""Large-document attachments, PR-1 (docs/specs/large-document-attachments.md §4C–D).

Three things are pinned here:

* ``is_reference_document`` is the one predicate for the reference class;
* the ``_derived/`` layout and the text layer's encoding (``large_document.py``)
  round-trip exactly, including texts ``str.splitlines`` would cut;
* ``document_read`` gives the **same answer** from a prepared document's
  sidecars as from the original: the sidecars are built by the real
  preparation code (``lambdas/large_document_prepare``), then every mode is run
  both ways and compared. Only the random tail of a slice's document name may
  differ.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict

import pytest

from apis.shared.files import document_read as dr
from apis.shared.files import large_document as ld
from apis.shared.files.models import (
    INLINE_DOCUMENT_MAX_BYTES,
    LARGE_DOCUMENT_MAX_BYTES,
    FileMetadata,
    FileStatus,
    is_reference_document,
)
from apis.shared.files.workspace import WorkspaceFileNotFoundError, WorkspaceValidationError
from lambdas.large_document_prepare import handler
from tests.shared.test_document_read import build_docx, build_pdf

MODULE = "apis.shared.files.document_read"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MB = 1024 * 1024


def _meta(filename="manual.pdf", mime="application/pdf", size=1000, preparation=None, upload_id="up-1"):
    return FileMetadata(
        upload_id=upload_id, user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/u1/s1/{upload_id}/{filename}", s3_bucket="b",
        status=FileStatus.READY, preparation=preparation,
    )


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


class TestIsReferenceDocument:
    @pytest.mark.parametrize(
        "filename,mime",
        [
            ("a.pdf", "application/pdf"),
            ("a.docx", DOCX),
            ("a.txt", "text/plain"),
            ("a.md", "text/markdown"),
            ("a.html", "text/html"),
        ],
    )
    def test_document_class_between_the_two_limits(self, filename, mime):
        assert is_reference_document(_meta(filename, mime, size=INLINE_DOCUMENT_MAX_BYTES + 1))
        assert is_reference_document(_meta(filename, mime, size=LARGE_DOCUMENT_MAX_BYTES))

    def test_limits_are_four_and_fifty_megabytes(self):
        assert INLINE_DOCUMENT_MAX_BYTES == 4 * MB
        assert LARGE_DOCUMENT_MAX_BYTES == 50 * MB

    @pytest.mark.parametrize("size", [1, INLINE_DOCUMENT_MAX_BYTES, LARGE_DOCUMENT_MAX_BYTES + 1])
    def test_inline_and_too_large_sizes_are_not_reference(self, size):
        assert not is_reference_document(_meta(size=size))

    @pytest.mark.parametrize(
        "filename,mime",
        [
            ("a.csv", "text/csv"),
            ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("a.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ("a.png", "image/png"),
        ],
    )
    def test_other_classes_are_never_reference(self, filename, mime):
        assert not is_reference_document(_meta(filename, mime, size=10 * MB))


# ---------------------------------------------------------------------------
# Layout and codec
# ---------------------------------------------------------------------------


class TestLayout:
    def test_sidecars_live_under_the_upload_folder(self):
        key = "user-files/u1/s1/up-1/My File.pdf"
        assert ld.derived_prefix(key) == "user-files/u1/s1/up-1/_derived/"
        assert ld.text_layer_key(key) == "user-files/u1/s1/up-1/_derived/text.jsonl"
        assert ld.shard_key(key, 11, 20) == "user-files/u1/s1/up-1/_derived/pages-0011-0020.pdf"

    def test_shard_ranges_cover_every_page_once(self):
        assert ld.shard_ranges(25, 10) == [(1, 10), (11, 20), (21, 25)]
        assert ld.shard_ranges(10, 10) == [(1, 10)]
        assert ld.shard_ranges(0, 10) == []

    @pytest.mark.parametrize(
        "start,end,expected",
        [(1, 1, [(1, 10)]), (9, 12, [(1, 10), (11, 20)]), (10, 29, [(1, 10), (11, 20), (21, 25)]), (21, 25, [(21, 25)])],
    )
    def test_covering_shards(self, start, end, expected):
        assert ld.covering_shards(start, end, 25, 10) == expected

    def test_text_layer_round_trips_texts_splitlines_would_cut(self):
        texts = ["plain", "", "line\nbreak", "form\x0cfeed", "sep arator", "quote \" and \\ back", "ünïcödé"]
        body = ld.encode_text_layer("page", texts)
        assert ld.decode_text_layer(body) == texts
        first = json.loads(body.split(b"\n")[0])
        assert first == {"page": 1, "text": "plain"}

    def test_is_prepared_needs_ready_and_this_version(self):
        assert ld.is_prepared(_meta(preparation=ld.preparation_record(status=ld.STATUS_READY)))
        assert not ld.is_prepared(_meta(preparation=ld.preparation_record(status=ld.STATUS_PENDING)))
        assert not ld.is_prepared(_meta(preparation={"status": "ready", "version": ld.PREPARATION_VERSION + 1}))
        assert not ld.is_prepared(_meta())

    def test_preparation_round_trips_through_the_row(self):
        prep = ld.preparation_record(status=ld.STATUS_READY, fmt="pdf", page_count=3, shard_pages=10, text_bytes=99)
        meta = _meta(preparation=prep)
        item = meta.to_dynamo_item()
        assert item["preparation"] == prep
        assert FileMetadata.from_dynamo_item(item).preparation == prep
        assert "preparation" not in _meta().to_dynamo_item()


# ---------------------------------------------------------------------------
# document_read parity: sidecars vs the original
# ---------------------------------------------------------------------------


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """Enough of S3 for the preparation code and document_read's fetch."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> Dict[str, Any]:
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: Any, ContentType: str = "") -> None:
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()


class FakeRepository:
    def __init__(self, meta: FileMetadata) -> None:
        self.meta = meta

    async def get_file(self, user_id: str, upload_id: str) -> FileMetadata:
        return self.meta

    async def update_file_preparation(self, user_id, upload_id, preparation, digest=None):
        self.meta = self.meta.model_copy(update={"preparation": preparation, "digest": digest})
        return self.meta


async def _prepare(meta: FileMetadata, raw: bytes, monkeypatch) -> tuple[FileMetadata, FakeS3]:
    """Run the real preparation over ``raw`` (sized as a reference document)."""
    monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "false")
    s3 = FakeS3()
    s3.objects[meta.s3_key] = raw
    large = meta.model_copy(update={"size_bytes": INLINE_DOCUMENT_MAX_BYTES + 1})
    repository = FakeRepository(large)
    assert await handler.prepare_upload("u1", meta.upload_id, s3=s3, repository=repository) == "ready"
    return repository.meta, s3


@pytest.fixture
def both(monkeypatch):
    """Serve one document two ways: the original (small, unprepared) and the
    prepared copy (reference-sized, read from sidecars). Returns a runner
    that reads it both ways and the S3 keys each read fetched."""
    state: Dict[str, Any] = {}

    async def _owned(user_id, upload_id):
        return state["current"]

    def _get(bucket, key):
        state["fetched"].append(key)
        return state["s3"].objects[key]

    monkeypatch.setattr(f"{MODULE}._get_owned_ready_file", _owned)
    monkeypatch.setattr(f"{MODULE}._get_object_bytes", _get)

    async def _setup(meta: FileMetadata, raw: bytes):
        prepared, s3 = await _prepare(meta, raw, monkeypatch)
        state["s3"] = s3
        state["original"], state["prepared"] = meta, prepared

    async def _read(**kwargs):
        out = {}
        for which in ("original", "prepared"):
            state["current"], state["fetched"] = state[which], []
            try:
                result = await dr.read_document("u1", "s1", "up-1", **kwargs)
            except Exception as e:  # noqa: BLE001 - the error is the answer
                result = (type(e), str(e).replace(str(state[which].size_bytes), "N"))
            out[which] = (result, list(state["fetched"]))
        return out

    return _setup, _read


def _comparable(result) -> Any:
    """A read result with the random slice-name tail, the size field (the
    prepared copy is reference-sized) and the document bytes normalized."""
    if isinstance(result, tuple):
        return result
    payload = dict(result.payload)
    payload.pop("size_bytes")
    block = None
    if result.document_block:
        doc = result.document_block["document"]
        pdf = dr._open_pdf(doc["source"]["bytes"])
        try:
            block = (doc["format"], doc["name"].rsplit(" ", 1)[0], [dr._pdf_page_text(pdf, i) for i in range(len(pdf))])
        finally:
            pdf.close()
    return (result.mode, payload, block, result.pages_returned, result.format, result.bytes_returned if not block else None)


PAGES = [f"Page {i} alpha beta" for i in range(1, 26)]
PAGES[16] = "The termination clause survives"
PAGES[22] = "Termination fees apply"


@pytest.mark.asyncio
class TestPdfParity:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"page_range": "3"},
            {"page_range": "9-12"},  # spans two shards
            {"page_range": "8-25", "max_pages": 20},  # three shards, hard cap untouched
            {"page_range": "1-25", "max_pages": 50},  # clamped to the hard cap
            {"page_range": "2-30", "max_pages": 4},  # truncated, with a continuation
            {"page_range": "20-30"},  # clipped to the end
            {"page_range": "26-27"},  # past the end
            {"page_range": "4-6", "text_only": True},
            {"pattern": "termination"},
            {"pattern": "termination", "page_range": "1-2"},  # pattern wins over a range
            {"pattern": "(a+)+$"},  # catastrophic: searched literally, with a note
            {"pattern": "[unclosed"},  # invalid: searched literally, with a note
            {"pattern": "alpha"},  # many hits: capped at the match limit
            {},  # index
        ],
    )
    async def test_every_mode_matches_the_original(self, both, kwargs):
        setup, read = both
        await setup(_meta(), build_pdf(PAGES))
        out = await read(**kwargs)
        assert _comparable(out["prepared"][0]) == _comparable(out["original"][0])

    async def test_page_range_fetches_only_the_covering_shards(self, both):
        setup, read = both
        await setup(_meta(), build_pdf(PAGES))
        out = await read(page_range="9-12")
        _, fetched = out["prepared"]
        # The covering shards are fetched in parallel threads, so their
        # order is scheduling's, not the code's.
        assert sorted(fetched) == [
            "user-files/u1/s1/up-1/_derived/pages-0001-0010.pdf",
            "user-files/u1/s1/up-1/_derived/pages-0011-0020.pdf",
        ]
        assert out["original"][1] == ["user-files/u1/s1/up-1/manual.pdf"]

    async def test_pattern_and_index_read_the_text_layer_not_the_pdf(self, both):
        setup, read = both
        await setup(_meta(), build_pdf(PAGES))
        for kwargs in ({"pattern": "termination"}, {}, {"page_range": "1-2", "text_only": True}):
            out = await read(**kwargs)
            assert out["prepared"][1] == ["user-files/u1/s1/up-1/_derived/text.jsonl"]

    async def test_index_is_capped_like_the_original(self, both, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.DOCUMENT_READ_INDEX_PAGES", 5)
        setup, read = both
        await setup(_meta(), build_pdf(PAGES))
        out = await read()
        assert _comparable(out["prepared"][0]) == _comparable(out["original"][0])
        assert out["prepared"][0].payload["index_truncated"] is True

    async def test_match_cap_and_time_budget_apply_to_the_sidecars(self, both, monkeypatch):
        setup, read = both
        await setup(_meta(), build_pdf(PAGES))
        monkeypatch.setattr(f"{MODULE}.DOCUMENT_READ_MAX_MATCHES", 3)
        out = await read(pattern="alpha")
        assert out["prepared"][0].payload["truncated"] is True
        assert out["prepared"][0].payload["match_count"] == 3

        ticks = iter(range(10_000))
        monkeypatch.setattr(f"{MODULE}.DOCUMENT_READ_MAX_MATCHES", 40)
        monkeypatch.setattr(f"{MODULE}.DOCUMENT_READ_PATTERN_BUDGET_SECONDS", 2.5)
        monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: float(next(ticks)))
        out = await read(pattern="alpha")
        payload = out["prepared"][0].payload
        assert payload["timed_out"] is True
        assert payload["pages_searched"] < len(PAGES)


@pytest.mark.asyncio
class TestTextFormatParity:
    DOCX_PARAS = [f"Paragraph {i} lorem ipsum" for i in range(1, 30)] + ["Termination survives"]

    @pytest.mark.parametrize(
        "kwargs",
        [{"pattern": "termination"}, {}, {"offset": 40}, {"pattern": "paragraph 2"}, {"page_range": "1"}],
    )
    async def test_docx_matches_the_original(self, both, kwargs):
        setup, read = both
        await setup(_meta("memo.docx", DOCX), build_docx(self.DOCX_PARAS))
        out = await read(**kwargs)
        assert _comparable(out["prepared"][0]) == _comparable(out["original"][0])
        if "pattern" in kwargs or not kwargs.get("page_range"):
            assert out["prepared"][1] == ["user-files/u1/s1/up-1/_derived/text.jsonl"]

    @pytest.mark.parametrize(
        "filename,mime,raw",
        [
            ("notes.txt", "text/plain", "intro\r\nTermination here\n\nlast line\x0cfeed sep\nTERMINATION end"),
            ("readme.md", "text/markdown", "# Title\n\nSome termination text\n## Part\nmore"),
            ("page.html", "text/html", "<h1>Head</h1>\n<p>termination clause</p>\n<p>other</p>"),
        ],
    )
    async def test_text_family_pattern_matches_the_original(self, both, filename, mime, raw):
        setup, read = both
        await setup(_meta(filename, mime), raw.encode("utf-8"))
        out = await read(pattern="termination")
        assert _comparable(out["prepared"][0]) == _comparable(out["original"][0])
        assert out["prepared"][0].payload["match_count"] >= 1
        assert out["prepared"][1] == ["user-files/u1/s1/up-1/_derived/text.jsonl"]

    async def test_text_family_text_mode_stays_a_ranged_read_of_the_original(self, both, monkeypatch):
        setup, read = both
        await setup(_meta("notes.txt", "text/plain"), b"hello\nworld")
        calls = []

        async def delegate(user_id, upload_id, offset=0):
            calls.append((upload_id, offset))
            return {"content": "hello", "offset": offset, "truncated": True, "next_offset": 5}

        monkeypatch.setattr(f"{MODULE}.read_workspace_file", delegate)
        out = await read(offset=3)
        assert _comparable(out["prepared"][0]) == _comparable(out["original"][0])
        assert calls == [("up-1", 3), ("up-1", 3)]
        assert out["prepared"][1] == []


# ---------------------------------------------------------------------------
# Reference-sized documents without usable sidecars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUnprepared:
    @pytest.fixture
    def serve(self, monkeypatch):
        state = {}

        async def _owned(user_id, upload_id):
            return state["meta"]

        def _get(bucket, key):
            raise AssertionError(f"no fetch expected, got {key}")

        monkeypatch.setattr(f"{MODULE}._get_owned_ready_file", _owned)
        monkeypatch.setattr(f"{MODULE}._get_object_bytes", _get)
        return state

    @pytest.mark.parametrize(
        "preparation,message",
        [
            (ld.preparation_record(status=ld.STATUS_PENDING), "still being prepared"),
            (ld.preparation_record(status=ld.STATUS_FAILED, error="PdfiumError"), "could not be prepared"),
            (None, "too large to read"),
        ],
    )
    async def test_over_the_source_cap_is_conversational_and_fetches_nothing(self, serve, preparation, message):
        serve["meta"] = _meta(size=20 * MB, preparation=preparation)
        with pytest.raises(dr.DocumentReadError, match=message):
            await dr.read_document("u1", "s1", "up-1", pattern="x")

    async def test_failed_preparation_under_the_source_cap_falls_back_to_the_original(self, serve, monkeypatch):
        raw = build_pdf(PAGES)
        monkeypatch.setattr(f"{MODULE}._get_object_bytes", lambda bucket, key: raw)
        serve["meta"] = _meta(
            size=5 * MB, preparation=ld.preparation_record(status=ld.STATUS_FAILED, error="ClientError")
        )
        result = await dr.read_document("u1", "s1", "up-1", pattern="termination")
        assert result.payload["pages_matched"] == [17, 23]

    async def test_unprepared_ownership_is_unchanged(self, monkeypatch):
        async def _missing(user_id, upload_id):
            raise WorkspaceFileNotFoundError("No file")

        monkeypatch.setattr(f"{MODULE}._get_owned_ready_file", _missing)
        with pytest.raises(WorkspaceFileNotFoundError):
            await dr.read_document("u1", "s1", "up-1")


def test_page_range_validation_is_shared():
    with pytest.raises(WorkspaceValidationError, match="has 25 pages"):
        dr._resolve_range((26, 27), 25, 8)
    assert dr._resolve_range((2, 30), 25, 4) == (2, 5, True)
    assert dr._resolve_range((20, 30), 25, 8) == (20, 25, False)


def test_assembled_shards_keep_page_order_and_identity():
    raw = build_pdf(PAGES)
    pdf = dr._open_pdf(raw)
    try:
        shards = []
        for start, end in ld.shard_ranges(len(PAGES), 10):
            out = io.BytesIO()
            handler._render_shard(pdf, start, end, out)
            shards.append(((start, end), out.getvalue()))
    finally:
        pdf.close()
    sliced = dr._assemble_from_shards(shards, 8, 22)
    doc = dr._open_pdf(sliced)
    try:
        assert [dr._pdf_page_text(doc, i) for i in range(len(doc))] == PAGES[7:22]
    finally:
        doc.close()


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("", True), ("true", True), ("1", True), ("yes", True), ("false", False), (" FALSE ", False)],
)
def test_feature_flag_is_on_unless_false(monkeypatch, value, expected):
    from apis.shared.feature_flags import large_document_attachments_enabled

    if value is None:
        monkeypatch.delenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", value)
    assert large_document_attachments_enabled() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,mime,raw,unit,texts",
    [
        ("m.pdf", "application/pdf", build_pdf(PAGES), "page", PAGES),
        ("m.docx", DOCX, build_docx(["1. Scope", "Body text here.", "2. Terms"]), "paragraph",
         ["1. Scope", "Body text here.", "2. Terms"]),
        ("m.md", "text/markdown", b"# Title\nbody\n## Part\nmore", "line", ["# Title", "body", "## Part", "more"]),
    ],
    # Fixed ids: the default embeds the raw bytes, and the .docx zip carries the time
    # it was built, so xdist workers collecting a second apart disagreed on the names.
    ids=["pdf", "docx", "md"],
)
async def test_digest_from_extracted_units_matches_the_digest_from_bytes(filename, mime, raw, unit, texts):
    """The Lambda hands build_digest the units it already extracted; the
    digest must be the one build_digest would have made from the bytes."""
    from apis.shared.files.document_digest import build_digest

    def comparable(d):
        return d.model_dump(exclude={"generated_at", "extractor_ms"})

    from_bytes = await build_digest(raw=raw, mime_type=mime, filename=filename, upload_id="u", with_abstract=False)
    from_units = await build_digest(
        raw=b"", mime_type=mime, filename=filename, upload_id="u", with_abstract=False, units=(unit, texts)
    )
    assert from_units.status == "ready"
    assert comparable(from_units) == comparable(from_bytes)
