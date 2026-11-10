"""The one document token estimator, read from row facts (unified-attachment-delivery §9.1, phase 2)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from apis.shared.files.document_estimate import (
    DocumentTokenFacts,
    clear_session_documents,
    document_tokens,
    facts_tokens,
    remember_session_documents,
    session_document_facts,
    token_facts,
)
from apis.shared.files.document_tokens import PDF_PAGE_TOKEN_ESTIMATE, estimate_document_tokens


@pytest.fixture(autouse=True)
def _clean():
    clear_session_documents()
    yield
    clear_session_documents()


def _row(upload_id="up-1", *, user_id="u1", filename="solicitation.pdf", size=134_392,
         file_format="pdf", facts="default"):
    if facts == "default":
        # What #1966 wrote for the 24-page PDF of the 2026-11-07 dev run, as
        # DynamoDB returns it (numbers as Decimal).
        facts = {
            "version": Decimal(1), "class": "document",
            "tokens": {"text": Decimal(29_954), "native": Decimal(36_000)},
            "pages": Decimal(24), "hasTextLayer": True, "units": "page",
        }
    return SimpleNamespace(
        upload_id=upload_id, user_id=user_id, filename=filename, size_bytes=size,
        file_format=file_format, facts=facts,
    )


class TestTokenFacts:
    def test_reads_a_row_written_by_the_prepare_lambda(self):
        facts = token_facts(_row())
        assert facts == DocumentTokenFacts(
            upload_id="up-1", filename="solicitation.pdf", size_bytes=134_392, file_format="pdf",
            text_tokens=29_954, native_tokens=36_000, pages=24,
        )

    @pytest.mark.parametrize("facts", [None, {}, {"version": 2, "tokens": {"text": 1, "native": 1}},
                                       {"version": 1}, {"version": 1, "tokens": {"text": None, "native": 4}}])
    def test_a_row_without_facts_this_reader_knows_has_none(self, facts):
        """Rows written before #1966, still being prepared, or of a newer version."""
        assert token_facts(_row(facts=facts)) is None


class TestFactsTokens:
    def test_a_native_pdf_is_its_text_plus_its_page_images(self):
        """§4.1: a native PDF costs its text layer and a per-page image. The
        dev run measured ~91k for this file; text + images is 66k, the page
        images alone (``tokens.native`` of version 1) 36k, bytes/4 33.6k."""
        assert facts_tokens(token_facts(_row())) == 29_954 + 36_000

    def test_a_pdf_delivered_as_text_is_its_text(self):
        assert facts_tokens(token_facts(_row()), as_text=True) == 29_954

    def test_a_document_read_slice_is_its_pages_share(self):
        assert facts_tokens(token_facts(_row()), pages_covered=8) == round(65_954 * 8 / 24)

    def test_a_slice_never_counts_more_pages_than_the_document_has(self):
        assert facts_tokens(token_facts(_row()), pages_covered=99) == 65_954

    def test_a_text_format_is_its_text_not_its_bytes(self):
        """A .docx is zipped: bytes/4 is ~9x low, and Bedrock extracts it to text."""
        row = _row(filename="memo.docx", file_format="docx", size=40_000, facts={
            "version": 1, "tokens": {"text": 90_000, "native": 10_000}, "pages": None,
        })
        assert facts_tokens(token_facts(row)) == 90_000


class TestDocumentTokens:
    def test_facts_win_over_the_bytes(self):
        assert document_tokens("pdf", b"%PDF-1.4", token_facts(_row())) == 65_954

    def test_without_facts_it_is_the_bytes_estimator(self):
        """A legacy row (or no row): exactly ``estimate_document_tokens``."""
        raw = b"x" * 4_000
        assert document_tokens("txt", raw) == estimate_document_tokens("txt", raw) == 1_000

    def test_an_unparseable_pdf_without_facts_is_bytes_over_four(self):
        assert document_tokens("pdf", b"not a pdf" * 100) == 225

    def test_never_raises(self):
        assert document_tokens("pdf", None) == 0


class TestSessionFacts:
    def test_facts_are_kept_per_user_and_session(self):
        assert remember_session_documents("u1", "s1", [_row()]) == 1
        assert [f.upload_id for f in session_document_facts("u1", "s1")] == ["up-1"]
        assert session_document_facts("u2", "s1") == [], "a session id alone does not name a conversation"
        assert session_document_facts("u1", "s2") == []

    def test_another_users_row_is_never_kept(self):
        assert remember_session_documents("u1", "s1", [_row(user_id="u2")]) == 0

    def test_a_row_without_facts_never_replaces_facts_read_earlier(self):
        remember_session_documents("u1", "s1", [_row()])
        remember_session_documents("u1", "s1", [_row(facts=None), None])
        assert session_document_facts("u1", "s1")[0].text_tokens == 29_954

    def test_newest_first_and_bounded(self):
        from apis.shared.files import document_estimate as de

        remember_session_documents("u1", "s1", [_row(f"up-{i}") for i in range(de._MAX_FILES_PER_SESSION + 5)])
        kept = session_document_facts("u1", "s1")
        assert len(kept) == de._MAX_FILES_PER_SESSION
        assert kept[0].upload_id == f"up-{de._MAX_FILES_PER_SESSION + 4}"

        for i in range(de._MAX_SESSIONS + 3):
            remember_session_documents("u1", f"x{i}", [_row()])
        assert len(de._SESSIONS) == de._MAX_SESSIONS

    def test_nothing_is_kept_without_an_identity(self):
        assert remember_session_documents(None, "s1", [_row()]) == 0
        assert session_document_facts("u1", None) == []


def test_page_estimate_is_the_shared_constant():
    """``tokens.native`` of version 1 is pages x this constant; the reader adds the text."""
    assert token_facts(_row()).native_tokens == 24 * PDF_PAGE_TOKEN_ESTIMATE
