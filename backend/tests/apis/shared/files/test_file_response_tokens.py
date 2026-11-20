"""``GET /files`` carries a document's token estimate from its row's facts (workflow-runs spec §19, PR-W.1)."""

from decimal import Decimal

from apis.shared.files.models import FileMetadata, FileResponse


def _meta(mime: str = "application/pdf", *, facts=None, page_count=None) -> FileMetadata:
    return FileMetadata(
        upload_id="up-1",
        user_id="u1",
        session_id="s1",
        filename="proposal.pdf",
        mime_type=mime,
        size_bytes=134_392,
        s3_key="k",
        s3_bucket="b",
        status="ready",
        facts=facts,
        page_count=page_count,
    )


def test_a_prepared_pdf_reports_text_plus_page_images_and_its_pages():
    facts = {"version": Decimal(1), "tokens": {"text": Decimal(29_954), "native": Decimal(36_000)}, "pages": Decimal(24)}
    resp = FileResponse.from_metadata(_meta(facts=facts)).model_dump(by_alias=True)
    assert (resp["estimatedTokens"], resp["pages"]) == (65_954, 24)


def test_a_docx_reports_its_text_only():
    facts = {"version": 1, "tokens": {"text": 4_000, "native": 9_000}, "pages": None}
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    resp = FileResponse.from_metadata(_meta(mime, facts=facts)).model_dump(by_alias=True)
    assert (resp["estimatedTokens"], resp["pages"]) == (4_000, None)


def test_a_file_without_facts_reports_none_and_never_reads_the_file():
    resp = FileResponse.from_metadata(_meta(page_count=12)).model_dump(by_alias=True)
    assert (resp["estimatedTokens"], resp["pages"]) == (None, 12)
