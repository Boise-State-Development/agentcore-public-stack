"""
Tests for DocumentHandler — document format detection and ContentBlock creation.

Validates: Requirements 9.1–9.5
"""

from agents.main_agent.multimodal.document_handler import DocumentHandler


class TestIsDocument:
    """Tests for DocumentHandler.is_document"""

    # Req 9.1: is_document returns True for all supported extensions
    def test_pdf(self):
        assert DocumentHandler.is_document("report.pdf") is True

    def test_csv(self):
        assert DocumentHandler.is_document("data.csv") is True

    def test_doc(self):
        assert DocumentHandler.is_document("letter.doc") is True

    def test_docx(self):
        assert DocumentHandler.is_document("letter.docx") is True

    def test_xls(self):
        assert DocumentHandler.is_document("sheet.xls") is True

    def test_xlsx(self):
        assert DocumentHandler.is_document("sheet.xlsx") is True

    def test_html(self):
        assert DocumentHandler.is_document("page.html") is True

    def test_txt(self):
        assert DocumentHandler.is_document("notes.txt") is True

    def test_md(self):
        assert DocumentHandler.is_document("readme.md") is True

    def test_case_insensitive(self):
        assert DocumentHandler.is_document("REPORT.PDF") is True

    # Req 9.2: is_document returns False for unsupported extensions
    def test_exe_unsupported(self):
        assert DocumentHandler.is_document("app.exe") is False

    def test_zip_unsupported(self):
        assert DocumentHandler.is_document("archive.zip") is False

    def test_py_unsupported(self):
        assert DocumentHandler.is_document("script.py") is False

    def test_no_extension(self):
        assert DocumentHandler.is_document("noext") is False


class TestGetDocumentFormat:
    """Tests for DocumentHandler.get_document_format"""

    # Req 9.3: correct format string for each supported extension
    def test_pdf_format(self):
        assert DocumentHandler.get_document_format("report.pdf") == "pdf"

    def test_csv_format(self):
        assert DocumentHandler.get_document_format("data.csv") == "csv"

    def test_doc_format(self):
        assert DocumentHandler.get_document_format("letter.doc") == "doc"

    def test_docx_format(self):
        assert DocumentHandler.get_document_format("letter.docx") == "docx"

    def test_xls_format(self):
        assert DocumentHandler.get_document_format("sheet.xls") == "xls"

    def test_xlsx_format(self):
        assert DocumentHandler.get_document_format("sheet.xlsx") == "xlsx"

    def test_html_format(self):
        assert DocumentHandler.get_document_format("page.html") == "html"

    def test_txt_format(self):
        assert DocumentHandler.get_document_format("notes.txt") == "txt"

    def test_md_format(self):
        assert DocumentHandler.get_document_format("readme.md") == "md"

    def test_case_insensitive(self):
        assert DocumentHandler.get_document_format("REPORT.PDF") == "pdf"

    # Req 9.4: defaults to "txt" for unrecognized extensions
    def test_default_txt_for_unknown(self):
        assert DocumentHandler.get_document_format("file.xyz") == "txt"

    def test_default_txt_for_no_extension(self):
        assert DocumentHandler.get_document_format("noext") == "txt"


class TestCreateContentBlock:
    """Tests for DocumentHandler.create_content_block"""

    # Req 9.5: returns dict with "document" key containing "format", "name", "source.bytes"
    def test_content_block_structure(self):
        data = b"%PDF-1.4 fake content"
        block = DocumentHandler.create_content_block(data, "report.pdf", "report_pdf")

        assert "document" in block
        assert block["document"]["format"] == "pdf"
        assert block["document"]["name"] == "report_pdf"
        assert "source" in block["document"]
        assert block["document"]["source"]["bytes"] == data

    def test_content_block_csv(self):
        data = b"col1,col2\nval1,val2"
        block = DocumentHandler.create_content_block(data, "data.csv", "data_csv")

        assert block["document"]["format"] == "csv"
        assert block["document"]["name"] == "data_csv"
        assert block["document"]["source"]["bytes"] == data

    def test_content_block_preserves_bytes(self):
        data = b"arbitrary bytes content"
        block = DocumentHandler.create_content_block(data, "notes.txt", "notes_txt")

        assert block["document"]["format"] == "txt"
        assert block["document"]["source"]["bytes"] is data


class TestAdmittedExtensionsReachTheTurn:
    """Every document extension upload admits must build a block, or the file
    is admitted and then dropped from the turn (unified-attachment-delivery §1.3)."""

    def test_sql_is_a_txt_document(self):
        assert DocumentHandler.is_document("schema.sql") is True
        assert DocumentHandler.get_document_format("SCHEMA.SQL") == "txt"

    def test_document_handler_covers_admitted_extensions(self):
        from apis.shared.files.models import ALLOWED_EXTENSIONS

        # pptx never goes inline (PowerPoint tools); images use ImageHandler.
        not_documents = {".pptx", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
        missing = {
            ext for ext in ALLOWED_EXTENSIONS if ext not in not_documents
        } - DocumentHandler.SUPPORTED_EXTENSIONS
        assert missing == set()
