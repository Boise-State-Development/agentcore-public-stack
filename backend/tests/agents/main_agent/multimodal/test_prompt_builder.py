"""
Tests for PromptBuilder — multimodal prompt assembly from text, images, and documents.

Validates: Requirements 11.1–11.8
"""

import base64

from agents.main_agent.multimodal.prompt_builder import PromptBuilder


def _make_builder() -> PromptBuilder:
    return PromptBuilder()


class TestBuildPromptNoFiles:
    """Req 11.1: When no files are provided, build_prompt returns the message as a plain string."""

    def test_no_files_returns_string(self):
        builder = _make_builder()
        result = builder.build_prompt("Hello world")
        assert result == "Hello world"
        assert isinstance(result, str)

    def test_empty_files_list_returns_string(self):
        builder = _make_builder()
        result = builder.build_prompt("Hello world", files=[])
        assert result == "Hello world"
        assert isinstance(result, str)

    def test_none_files_returns_string(self):
        builder = _make_builder()
        result = builder.build_prompt("Hello world", files=None)
        assert result == "Hello world"


class TestBuildPromptWithImages:
    """Req 11.2, 11.3: With files, returns list of ContentBlocks; image files produce image blocks."""

    def test_returns_list_with_text_first(self, sample_files):
        builder = _make_builder()
        image_file = sample_files[0]  # photo.png
        result = builder.build_prompt("Describe this", files=[image_file])

        assert isinstance(result, list)
        assert len(result) >= 2
        # Text block is first
        assert "text" in result[0]

    def test_image_block_has_correct_format(self, sample_files):
        builder = _make_builder()
        image_file = sample_files[0]  # photo.png, image/png
        result = builder.build_prompt("Describe this", files=[image_file])

        image_blocks = [b for b in result if "image" in b]
        assert len(image_blocks) == 1
        assert image_blocks[0]["image"]["format"] == "png"
        assert "source" in image_blocks[0]["image"]
        assert "bytes" in image_blocks[0]["image"]["source"]


class TestBuildPromptWithDocuments:
    """Req 11.4: Document files produce document blocks with sanitized names."""

    def test_document_block_structure(self, sample_files):
        builder = _make_builder()
        doc_file = sample_files[1]  # report.pdf
        result = builder.build_prompt("Summarize this", files=[doc_file])

        assert isinstance(result, list)
        doc_blocks = [b for b in result if "document" in b]
        assert len(doc_blocks) == 1
        assert doc_blocks[0]["document"]["format"] == "pdf"
        assert "name" in doc_blocks[0]["document"]
        assert "source" in doc_blocks[0]["document"]

    def test_document_name_is_sanitized(self):
        """Document names go through FileSanitizer (dots replaced with underscores)."""
        builder = _make_builder()
        raw = base64.b64encode(b"content").decode()
        from tests.agents.main_agent.conftest import FakeFileContent

        file = FakeFileContent(
            filename="my report.final.pdf",
            content_type="application/pdf",
            bytes=raw,
        )
        result = builder.build_prompt("Read this", files=[file])

        doc_blocks = [b for b in result if "document" in b]
        assert len(doc_blocks) == 1
        # FileSanitizer replaces dots with underscores
        sanitized_name = doc_blocks[0]["document"]["name"]
        assert "." not in sanitized_name


class TestBuildPromptUnsupportedFiles:
    """Req 11.5: Unsupported file types are skipped."""

    def test_unsupported_file_skipped(self, sample_files):
        builder = _make_builder()
        unsupported_file = sample_files[2]  # script.py, text/x-python
        result = builder.build_prompt("Process this", files=[unsupported_file])

        assert isinstance(result, list)
        # Only text block, no image or document blocks
        assert len(result) == 1
        assert "text" in result[0]

    def test_sql_file_becomes_a_txt_document_block(self):
        # .sql is admitted as text/plain; it used to be dropped here.
        import base64 as _b64

        from tests.agents.main_agent.conftest import FakeFileContent

        file = FakeFileContent(
            "schema.sql", "text/plain", _b64.b64encode(b"SELECT 1;").decode()
        )
        result = _make_builder().build_prompt("Read this", files=[file])
        docs = [b["document"] for b in result if "document" in b]
        assert len(docs) == 1
        assert docs[0]["format"] == "txt"
        assert docs[0]["source"]["bytes"] == b"SELECT 1;"

    def test_mixed_files_skips_unsupported(self, sample_files):
        builder = _make_builder()
        # All three: image, document, unsupported
        result = builder.build_prompt("Process all", files=sample_files)

        assert isinstance(result, list)
        image_blocks = [b for b in result if "image" in b]
        doc_blocks = [b for b in result if "document" in b]
        text_blocks = [b for b in result if "text" in b]

        assert len(text_blocks) == 1
        assert len(image_blocks) == 1
        assert len(doc_blocks) == 1
        # Total = text + image + document (unsupported skipped)
        assert len(result) == 3


class TestGetContentTypeSummary:
    """Req 11.6, 11.7: get_content_type_summary returns correct descriptions."""

    def test_text_only_for_string(self):
        builder = _make_builder()
        assert builder.get_content_type_summary("Hello") == "text only"

    def test_multimodal_with_images(self, sample_files):
        builder = _make_builder()
        image_file = sample_files[0]
        prompt = builder.build_prompt("Describe", files=[image_file])
        summary = builder.get_content_type_summary(prompt)

        assert "text" in summary
        assert "1 image" in summary

    def test_multimodal_with_document(self, sample_files):
        builder = _make_builder()
        doc_file = sample_files[1]
        prompt = builder.build_prompt("Summarize", files=[doc_file])
        summary = builder.get_content_type_summary(prompt)

        assert "text" in summary
        assert "1 document" in summary

    def test_multimodal_mixed(self, sample_files):
        builder = _make_builder()
        prompt = builder.build_prompt("Process", files=sample_files)
        summary = builder.get_content_type_summary(prompt)

        assert "text" in summary
        assert "1 image" in summary
        assert "1 document" in summary

    def test_plural_images(self):
        builder = _make_builder()
        raw = base64.b64encode(b"data").decode()
        from tests.agents.main_agent.conftest import FakeFileContent

        files = [
            FakeFileContent("a.png", "image/png", raw),
            FakeFileContent("b.jpg", "image/jpeg", raw),
        ]
        prompt = builder.build_prompt("Describe", files=files)
        summary = builder.get_content_type_summary(prompt)

        assert "2 images" in summary


class TestAttachedFilesMarker:
    """Req 11.8: Text block includes '[Attached files: ...]' marker when files have filenames."""

    def test_marker_present_single_file(self, sample_files):
        builder = _make_builder()
        result = builder.build_prompt("Hello", files=[sample_files[0]])

        text_block = result[0]
        assert "[Attached files:" in text_block["text"]
        assert "photo.png" in text_block["text"]

    def test_marker_lists_all_filenames(self, sample_files):
        builder = _make_builder()
        result = builder.build_prompt("Hello", files=sample_files)

        text_block = result[0]
        assert "[Attached files:" in text_block["text"]
        assert "photo.png" in text_block["text"]
        assert "report.pdf" in text_block["text"]
        assert "script.py" in text_block["text"]

    def test_original_message_preserved_in_text_block(self, sample_files):
        builder = _make_builder()
        result = builder.build_prompt("My message", files=[sample_files[0]])

        text_block = result[0]
        assert text_block["text"].startswith("My message")
