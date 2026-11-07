"""A Converse request never carries more than five non-PDF documents.

Bedrock rejects the whole request past five ("You can't include more than 5
documents in a request"), counted over every message and every tool result,
with PDFs exempt (probed on dev across Claude and Nova, 2026-11-06). A warm
session's earlier Word or text attachments stay inline while offload waits,
so a later turn's attachments tipped prod turns over and force-stopped them.
``converse_document_cap`` projects each request; these tests pin that it keeps
the first five, sends the rest as text, never touches the shared list, and
keeps a projected prefix byte-stable as the conversation grows.
"""

import asyncio
import copy
import io
import json
import zipfile
from unittest.mock import MagicMock

import pytest
from strands.models import BedrockModel

from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
from apis.shared.models import document_cap
from apis.shared.models.document_cap import (
    CONVERSE_MAX_NON_PDF_DOCUMENTS,
    converse_document_cap,
    project_document_cap,
    render_document_as_text,
)


def _doc(fmt: str, name: str, data: bytes = b"hello") -> dict:
    return {"document": {"format": fmt, "name": name, "source": {"bytes": data}}}


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


def _user(*blocks, text: str = "Compare these.") -> dict:
    return {"role": "user", "content": [*copy.deepcopy(list(blocks)), {"text": text}]}


def _assistant(text: str = "Done.") -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def _documents(messages):
    """``(format, name)`` of every document block, tool results included, in order."""
    found = []
    for message in messages:
        for block in message["content"]:
            if "document" in block:
                found.append((block["document"]["format"], block["document"]["name"]))
            for item in block.get("toolResult", {}).get("content", []):
                if "document" in item:
                    found.append((item["document"]["format"], item["document"]["name"]))
    return found


def _non_pdf(messages):
    return [d for d in _documents(messages) if d[0] != "pdf"]


def _prod_shape():
    """The prod failure: five Word files last turn, still inline, five more now."""
    return [
        _user(*[_doc("docx", f"Interview {i}", _docx(f"notes {i}")) for i in range(5)]),
        _assistant("Here is the summary."),
        _user(*[_doc("docx", f"Focus group {i}", _docx(f"transcript {i}")) for i in range(5)]),
    ]


class TestProjection:
    def test_the_limit_is_bedrocks_five(self):
        assert CONVERSE_MAX_NON_PDF_DOCUMENTS == 5

    def test_a_request_within_the_limit_is_returned_as_is(self):
        history = [_user(*[_doc("docx", f"d{i}") for i in range(5)]), _assistant(), _user(text="More?")]
        assert project_document_cap(history) is history

    def test_text_history_is_returned_as_is(self):
        history = [_user(text="hi"), _assistant()]
        assert project_document_cap(history) is history

    def test_pdfs_do_not_count(self):
        history = [_user(*[_doc("pdf", f"p{i}") for i in range(20)], *[_doc("txt", f"t{i}") for i in range(5)])]
        assert project_document_cap(history) is history

    def test_the_prod_shape_keeps_the_first_five_and_sends_the_rest_as_text(self):
        history = _prod_shape()
        projected = project_document_cap(history)

        assert _non_pdf(projected) == [("docx", f"Interview {i}") for i in range(5)]
        new_turn = projected[2]["content"]
        assert new_turn[0] == {
            "text": '<document name="Focus group 0" format="docx">\ntranscript 0\n</document>'
        }
        assert new_turn[-1] == {"text": "Compare these."}, "the user's own text is untouched"

    def test_counts_across_tool_results(self):
        uses = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "read", "input": {}}}]}
        result = {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        "content": [{"text": "two files"}, _doc("txt", "a", b"alpha"), _doc("md", "b", b"# beta")],
                    }
                }
            ],
        }
        history = [_user(*[_doc("txt", f"t{i}") for i in range(4)]), uses, result]

        projected = project_document_cap(history)

        assert len(_non_pdf(projected)) == 5
        items = projected[2]["content"][0]["toolResult"]["content"]
        assert items[1] == history[2]["content"][0]["toolResult"]["content"][1], "the fifth stays a document"
        assert items[2] == {"text": '<document name="b" format="md">\n# beta\n</document>'}
        assert projected[2]["content"][0]["toolResult"]["toolUseId"] == "t1"
        assert projected[2]["content"][0]["toolResult"]["status"] == "success"

    def test_pdfs_after_the_limit_stay_documents(self):
        history = [_user(*[_doc("txt", f"t{i}") for i in range(6)], _doc("pdf", "scan"))]
        projected = project_document_cap(history)
        assert _documents(projected)[-1] == ("pdf", "scan")
        assert len(_non_pdf(projected)) == 5

    def test_never_mutates_the_shared_history(self):
        history = _prod_shape()
        snapshot = copy.deepcopy(history)
        project_document_cap(history)
        assert history == snapshot

    def test_untouched_messages_are_shared_not_copied(self):
        history = _prod_shape()
        projected = project_document_cap(history)
        assert projected[0] is history[0]
        assert projected[1] is history[1]
        assert projected[2] is not history[2]

    def test_tolerates_odd_shapes(self):
        history = [
            {"role": "user", "content": "plain string"},
            {"role": "user"},
            "not a dict",
            {"role": "user", "content": [None, {"toolResult": "odd"}, {"toolResult": {"content": "odd"}}]},
            _user(*[_doc("txt", f"t{i}") for i in range(6)]),
        ]
        projected = project_document_cap(history)
        assert projected[:4] == history[:4]
        assert len(_non_pdf(projected[4:])) == 5


class TestPrefixStability:
    """The prompt-cache contract: a projected prefix never changes as turns append."""

    @staticmethod
    def _bytes(messages):
        return json.dumps(messages, default=lambda b: b.decode("latin-1"), sort_keys=True)

    def test_appending_turns_never_changes_the_projected_prefix(self):
        history = _prod_shape()
        first = project_document_cap(history)

        grown = history + [_assistant("Compared."), _user(_doc("txt", "late", b"late file"))]
        second = project_document_cap(grown)

        assert self._bytes(second[: len(first)]) == self._bytes(first)
        assert second[-1]["content"][0]["text"].startswith('<document name="late" format="txt">')

    def test_byte_stable_across_calls(self):
        assert self._bytes(project_document_cap(_prod_shape())) == self._bytes(project_document_cap(_prod_shape()))

    def test_a_slot_freed_earlier_brings_a_later_document_back_inline(self):
        """Offload swaps an early document for its digest. That re-write sits
        before every block whose fate it changes, so a later document moving
        back inline costs nothing extra."""
        history = _prod_shape()
        history[0]["content"][0] = {"text": "[digest of Interview 0]"}

        projected = project_document_cap(history)

        assert _non_pdf(projected) == [("docx", f"Interview {i}") for i in range(1, 5)] + [("docx", "Focus group 0")]


class TestRendering:
    def test_text_formats_are_decoded(self):
        for fmt in ("txt", "md", "csv", "html"):
            text = render_document_as_text(_doc(fmt, "f", "café".encode())["document"])
            assert text == f'<document name="f" format="{fmt}">\ncafé\n</document>'

    def test_docx_uses_the_document_read_extractor(self):
        text = render_document_as_text(_doc("docx", "minutes", _docx("First.", "Second."))["document"])
        assert text == '<document name="minutes" format="docx">\nFirst.\n\nSecond.\n</document>'

    @pytest.mark.parametrize("fmt", ["xlsx", "xls", "doc"])
    def test_formats_without_an_extractor_become_a_stub(self, fmt):
        text = render_document_as_text(_doc(fmt, "budget", b"\x00\x01")["document"])
        assert text.startswith(f'[Document "budget" ({fmt}) is not shown here.')
        assert "5 non-PDF documents" in text

    def test_a_corrupt_docx_becomes_a_stub(self):
        assert render_document_as_text(_doc("docx", "broken", b"not a zip")["document"]).startswith(
            '[Document "broken" (docx) is not shown here.'
        )

    def test_a_missing_name_or_bytes_still_renders(self):
        assert render_document_as_text({"format": "txt", "source": {}}).startswith('[Document "document" (txt)')

    def test_docx_text_is_memoized_by_the_bytes_object(self, monkeypatch):
        raw = _docx("Once.")
        calls = []
        from apis.shared.files import document_read

        real = document_read.docx_paragraphs
        monkeypatch.setattr(document_read, "docx_paragraphs", lambda b: calls.append(1) or real(b))
        monkeypatch.setattr(document_cap, "_TEXT_CACHE", type(document_cap._TEXT_CACHE)())
        monkeypatch.setattr(document_cap, "_text_cache_bytes", 0)

        block = _doc("docx", "m", raw)["document"]
        assert render_document_as_text(block) == render_document_as_text(block)
        assert len(calls) == 1

        same_content_other_object = bytes(bytearray(raw))
        render_document_as_text(_doc("docx", "m", same_content_other_object)["document"])
        assert len(calls) == 2, "a different bytes object is never served from another's entry"

    def test_the_memo_is_bounded(self, monkeypatch):
        monkeypatch.setattr(document_cap, "_TEXT_CACHE", type(document_cap._TEXT_CACHE)())
        monkeypatch.setattr(document_cap, "_text_cache_bytes", 0)
        kept = [_docx(f"p{i}") for i in range(document_cap._TEXT_CACHE_MAX_ENTRIES + 5)]
        for raw in kept:
            render_document_as_text(_doc("docx", "m", raw)["document"])
        assert len(document_cap._TEXT_CACHE) == document_cap._TEXT_CACHE_MAX_ENTRIES
        assert document_cap._text_cache_bytes == sum(len(e[0]) for e in document_cap._TEXT_CACHE.values())


class _RecordingModel:
    def __init__(self):
        self.streamed = self.counted = self.natively_counted = None

    async def stream(self, messages, *args, **kwargs):
        self.streamed = messages
        yield {"messageStart": {"role": "assistant"}}

    async def count_tokens(self, messages, *args, **kwargs):
        self.counted = messages
        return 7

    def native_count_tokens(self, messages, *args, **kwargs):
        self.natively_counted = messages
        return 9


class TestModelWrapper:
    @pytest.mark.asyncio
    async def test_stream_and_counts_see_the_projection(self):
        model = converse_document_cap(_RecordingModel())
        history = _prod_shape()

        [e async for e in model.stream(history, None, "system")]
        assert len(_non_pdf(model.streamed)) == 5
        await model.count_tokens(history)
        assert len(_non_pdf(model.counted)) == 5
        model.native_count_tokens(history)
        assert len(_non_pdf(model.natively_counted)) == 5

        assert len(_non_pdf(history)) == 10, "the caller's list is untouched"

    def test_bedrock_request_stays_within_the_limit_and_a_bedrock_model(self):
        """Through the real Converse formatter: what Bedrock would receive."""
        model = converse_document_cap(
            CountTokensBedrockModel(
                native_projection=False, model_id="global.anthropic.claude-sonnet-5", region_name="us-west-2"
            )
        )
        assert isinstance(model, BedrockModel)

        captured = {}
        original_format = model.format_request

        def spy(messages, *args, **kwargs):
            captured["request"] = original_format(messages, *args, **kwargs)
            raise RuntimeError("stop before the network")

        model.format_request = spy
        model.client = MagicMock()

        async def drain():
            async for _ in model.stream(_prod_shape(), None, "system"):
                pass

        with pytest.raises(RuntimeError, match="stop before the network"):
            asyncio.run(drain())

        sent = captured["request"]["messages"]
        assert len(_non_pdf(sent)) == 5
        assert any(
            block.get("text", "").startswith('<document name="Focus group 4"') for block in sent[-1]["content"]
        )
