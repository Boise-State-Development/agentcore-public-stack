"""The legacy pipeline hands Docling an annotated copy of a tracked-changes .docx.

Docling is not installed in the test environment (it lives in the rag-ingestion
image only), so its modules are stubbed and the converter records the bytes it
was given. That is the whole contract here: what Docling does with a well-formed
.docx is Docling's business; which .docx it receives is ours.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from apis.app_api.documents.ingestion.processors import docling_processor
from tests.shared.docx_fixtures import (
    build_docx,
    deleted,
    inserted,
    paragraph,
    python_docx_paragraphs,
    run,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _Converted(Exception):
    """Raised by the fake converter to stop the pipeline once the input is seen."""

    def __init__(self, data: bytes) -> None:
        super().__init__("converted")
        self.data = data


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


@pytest.fixture()
def converter_input(monkeypatch):
    """Stub torch/docling and capture the bytes written for conversion."""

    class _FakeConverter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def convert(self, path: str):
            raise _Converted(Path(path).read_bytes())

    stub = lambda *a, **k: None  # noqa: E731
    modules = {
        "torch": _module("torch", backends=types.SimpleNamespace()),
        "docling": _module("docling"),
        "docling.chunking": _module("docling.chunking", HybridChunker=stub),
        "docling.datamodel": _module("docling.datamodel"),
        "docling.datamodel.base_models": _module(
            "docling.datamodel.base_models", InputFormat=types.SimpleNamespace(PDF="pdf")
        ),
        "docling.datamodel.pipeline_options": _module(
            "docling.datamodel.pipeline_options",
            PdfPipelineOptions=stub,
            TableStructureOptions=stub,
        ),
        "docling.document_converter": _module(
            "docling.document_converter", DocumentConverter=_FakeConverter, PdfFormatOption=stub
        ),
        "docling_core": _module("docling_core"),
        "docling_core.transforms": _module("docling_core.transforms"),
        "docling_core.transforms.chunker": _module("docling_core.transforms.chunker"),
        "docling_core.transforms.chunker.tokenizer": _module(
            "docling_core.transforms.chunker.tokenizer"
        ),
        "docling_core.transforms.chunker.tokenizer.openai": _module(
            "docling_core.transforms.chunker.tokenizer.openai", OpenAITokenizer=stub
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    async def _convert(data: bytes, filename: str = "redline.docx", mime: str = DOCX_MIME) -> bytes:
        with pytest.raises(_Converted) as caught:
            await docling_processor.process_with_docling(data, mime, filename)
        return caught.value.data

    return _convert


@pytest.mark.asyncio
async def test_a_redline_is_converted_from_an_annotated_copy(converter_input):
    redline = build_docx(
        paragraph(run("Payment within "), deleted("thirty (30)"), inserted("ninety (90)"), run(" days."))
    )

    converted = await converter_input(redline)

    assert converted != redline
    assert python_docx_paragraphs(converted)[1:] == [
        "Payment within [deleted: thirty (30)][inserted: ninety (90)] days."
    ]


@pytest.mark.asyncio
async def test_a_docx_without_revisions_is_converted_from_its_original_bytes(converter_input):
    plain = build_docx(paragraph(run("Nothing changed here.")))

    assert await converter_input(plain) == plain


@pytest.mark.asyncio
async def test_a_docx_identified_by_mime_type_alone_is_annotated(converter_input):
    redline = build_docx(paragraph(deleted("old"), inserted("new")))

    converted = await converter_input(redline, filename=None)

    assert "[deleted: old][inserted: new]" in python_docx_paragraphs(converted)[1]
