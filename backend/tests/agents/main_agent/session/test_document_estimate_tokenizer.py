"""Compaction prices documents and text at the turn model's tokenizer, the planner's numbers.

``docs/specs/unified-attachment-delivery.md`` §2.1. On dev on 2026-11-10 the
32-page ``delivery_report_30k.pdf`` (``tokens.text`` 30,779) billed 29,298
tokens of text on Haiku 4.5 and Sonnet 4.6 and 43,225 on Haiku 5.5, Opus 4.7 and
Opus 5.5, plus ~1,565 a page for the page images. Compaction's document
estimate read the facts at factor 1.0 and 1,500 a page, so it under-counted the
newer tokenizer's text by up to 1.4x. Both now read ``tokenizer_costs``.
"""

import pytest

from agents.main_agent.session.compaction_policy import estimate_message_tokens
from agents.main_agent.session.document_context import summarize_document_context
from agents.main_agent.session.document_estimate import DocumentIndex
from agents.main_agent.session.turn_based_session_manager import _agent_model_id
from apis.shared.files import attachment_plan as ap
from apis.shared.files import tokenizer_costs as tc
from apis.shared.files.document_estimate import DocumentTokenFacts, facts_tokens

HAIKU45 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET46 = "us.anthropic.claude-sonnet-4-6"
HAIKU55 = "us.anthropic.claude-haiku-5-5"
OPUS47 = "us.anthropic.claude-opus-4-7"
OPUS55 = "us.anthropic.claude-opus-5-5"
SONNET5 = "global.anthropic.claude-sonnet-5"

#: (model, measured text tokens of the fixture) from the 2026-11-10 probe.
MEASURED_TEXT = [
    (HAIKU45, 29_298),
    (SONNET46, 29_298),
    (HAIKU55, 43_225),
    (OPUS47, 43_225),
    (OPUS55, 43_225),
]
FAMILIES = [
    (HAIKU45, 0.95),
    (SONNET46, 0.95),
    (HAIKU55, 1.40),
    (OPUS47, 1.40),
    (OPUS55, 1.40),
    (SONNET5, 1.40),  # not reachable from dev; the unlisted default
]

REPORT = DocumentTokenFacts(
    upload_id="up-report", filename="delivery_report_30k.pdf", size_bytes=200_000, file_format="pdf",
    text_tokens=30_779, native_tokens=48_000, pages=32,
)
MEMO = DocumentTokenFacts(
    upload_id="up-memo", filename="memo.docx", size_bytes=30_000, file_format="docx",
    text_tokens=40_000, native_tokens=7_500,
)


def _block(facts, fmt, name, size):
    return {"format": fmt, "name": name, "source": {"bytes": b"x" * size}}


REPORT_BLOCK = _block(REPORT, "pdf", "delivery_report_30k_pdf", REPORT.size_bytes)
MEMO_BLOCK = _block(MEMO, "docx", "memo_docx", MEMO.size_bytes)


class TestOneSharedFactor:
    def test_the_planner_reads_the_shared_module(self):
        assert ap.tokenizer_factor is tc.tokenizer_factor
        assert ap.PDF_PAGE_IMAGE_TOKENS == tc.PDF_PAGE_IMAGE_TOKENS == 1_565

    @pytest.mark.parametrize("model_id,factor", FAMILIES)
    def test_factor_per_family(self, model_id, factor):
        assert tc.tokenizer_factor(model_id) == factor
        assert DocumentIndex(model_id=model_id).text_factor == factor

    def test_no_model_keeps_the_facts_own_count(self):
        assert DocumentIndex().text_factor == 1.0
        assert facts_tokens(REPORT) == 30_779 + 48_000
        assert facts_tokens(MEMO) == 40_000


class TestDocumentsPerModel:
    @pytest.mark.parametrize("model_id,measured", MEASURED_TEXT)
    def test_text_is_within_one_percent_of_the_probe(self, model_id, measured):
        text = facts_tokens(REPORT, as_text=True, model_id=model_id)
        assert abs(text - measured) <= 0.01 * measured, (model_id, text)

    @pytest.mark.parametrize("model_id,factor", FAMILIES)
    def test_native_pdf_is_text_plus_page_images(self, model_id, factor):
        expected = tc.text_cost(30_779, factor) + 32 * 1_565
        assert facts_tokens(REPORT, model_id=model_id) == expected
        assert DocumentIndex([REPORT], model_id=model_id).block_tokens(REPORT_BLOCK) == expected

    @pytest.mark.parametrize("model_id,_factor", FAMILIES)
    def test_compaction_and_the_planner_agree(self, model_id, _factor):
        """The same facts, priced by the planner and by compaction's index, are one number."""
        model = ap.ModelFacts(text_only=False, converse=True, model_id=model_id)
        pdf = ap.FileFacts(
            filename="delivery_report_30k.pdf", mime_type="application/pdf", size_bytes=REPORT.size_bytes,
            page_count=32, text_tokens=30_779, native_tokens=48_000,
        )
        docx = ap.FileFacts(
            filename="memo.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=MEMO.size_bytes, text_tokens=40_000, native_tokens=7_500,
        )
        index = DocumentIndex([REPORT, MEMO], model_id=model_id)
        assert index.block_tokens(REPORT_BLOCK) == ap.document_cost(pdf, ap.CLASS_PDF, ap.MODE_NATIVE, model)
        assert index.block_tokens(MEMO_BLOCK) == ap.document_cost(docx, ap.CLASS_DOCUMENT, ap.MODE_TEXT, model)

    def test_a_slice_is_its_share_at_the_model_rate(self):
        whole = facts_tokens(REPORT, model_id=HAIKU55)
        assert facts_tokens(REPORT, pages_covered=8, model_id=HAIKU55) == round(whole * 8 / 32)

    def test_haiku_55_no_longer_under_counts(self):
        """Factor 1.0 and 1,500 a page was 78,779 for this PDF; Haiku 5.5 bills ~93.3k."""
        measured = 43_225 + 32 * 1_565
        old = facts_tokens(REPORT)
        new = facts_tokens(REPORT, model_id=HAIKU55)
        assert old < 0.85 * measured
        assert abs(new - measured) <= 0.01 * measured


class TestTextWeighsTheSame:
    @pytest.mark.parametrize("model_id,factor", FAMILIES)
    def test_message_text_scales_with_the_tokenizer(self, model_id, factor):
        message = {"role": "user", "content": [{"text": "a" * 40_000}]}
        assert estimate_message_tokens(message, DocumentIndex(model_id=model_id)) == round(10_000 * factor) + 4

    def test_images_do_not_scale(self):
        message = {"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b"x"}}}]}
        assert estimate_message_tokens(message, DocumentIndex(model_id=HAIKU55)) == estimate_message_tokens(message)

    def test_the_cost_row_footprint_reads_the_model_rate(self):
        messages = [{"role": "user", "content": [{"text": "read"}, {"document": REPORT_BLOCK}]}]
        footprint = summarize_document_context(messages, DocumentIndex([REPORT], model_id=HAIKU55))
        assert footprint["documentTokens"] == facts_tokens(REPORT, model_id=HAIKU55)


class TestTheSessionManagerPassesTheModel:
    def test_document_index_carries_the_model(self, make_session_manager):
        session_manager = make_session_manager()
        assert session_manager.document_index(HAIKU55).model_id == HAIKU55
        assert session_manager.document_index().model_id is None

    def test_the_agent_model_id_is_read_from_strands_config(self):
        class _Model:
            config = {"model_id": OPUS55}

        class _Agent:
            model = _Model()

        assert _agent_model_id(_Agent()) == OPUS55
        assert _agent_model_id(object()) is None
