"""The route carries out the attachment planner's ``token`` profile (phase 3).

``_resolve_turn_attachments`` plans before the model is known and fetches the
inline set; once the model is resolved, ``_carry_out_token_plan`` moves every
file the planner judged over the turn's ``document_budget`` out of the inline
set and into the by-reference list, rendered from the row already in hand.
These tests pin that move, the row facts the planner reads, and that the
``legacy`` profile is untouched.
"""

from __future__ import annotations

import base64

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.routes import TurnAttachments, TurnModel, _carry_out_token_plan, _file_facts
from apis.shared.files import models as file_models
from apis.shared.files.attachment_plan import (
    MODE_NATIVE,
    MODE_ON_DEMAND,
    MODE_TEXT,
    REASON_OVER_DOCUMENT_BUDGET,
    legacy_profile,
    plan_attachments,
    token_profile,
)
from apis.shared.files.reference_attachments import ReferenceAttachment
from apis.inference_api.chat.models import FileContent

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _row(upload_id: str, filename: str, mime: str, *, text: int, pages=None) -> file_models.FileMetadata:
    return file_models.FileMetadata(
        upload_id=upload_id,
        user_id="user-1",
        session_id="sess-1",
        filename=filename,
        mime_type=mime,
        size_bytes=20_000,
        s3_key="k",
        s3_bucket="b",
        status=file_models.FileStatus.READY,
        facts={
            "version": 1, "class": "document", "tokens": {"text": text, "native": 1_500 * (pages or 0)},
            "pages": pages, "hasTextLayer": True, "units": "page" if pages else "paragraph",
        },
    )


def _model(model_id: str, window: int) -> TurnModel:
    return TurnModel(
        model_id=model_id, provider="bedrock", caching_enabled=None, inference_params={},
        mantle_api_mode=None, mantle_region=None, input_modalities=["TEXT", "IMAGE"], context_window=window,
    )


HAIKU55 = ("us.anthropic.claude-haiku-5-5", 100_000)
SONNET46 = ("us.anthropic.claude-sonnet-4-6", 1_000_000)


def _attachments(rows, profile) -> TurnAttachments:
    files = [FileContent(filename=r.filename, content_type=r.mime_type, bytes=base64.b64encode(b"x" * 100).decode()) for r in rows]
    upload_rows = {id(f): r for f, r in zip(files, rows)}
    facts = [_file_facts(f, upload_rows) for f in files]
    plan = plan_attachments(facts, None, profile=profile)
    inline = plan.indices(MODE_NATIVE, MODE_TEXT)
    return TurnAttachments(
        files_to_send=[files[i] for i in inline], marker_names=[r.filename for r in rows],
        plan=plan, plan_facts=facts, inline_indices=inline, profile=profile, upload_rows=upload_rows,
    )


@pytest.fixture(autouse=True)
def _compaction(monkeypatch):
    monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "true")
    for name in ("AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS", "AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO"):
        monkeypatch.delenv(name, raising=False)


def test_the_row_facts_reach_the_planner():
    row = _row("up-a", "a.pdf", PDF, text=9_600, pages=10)
    f = FileContent(filename="a.pdf", content_type=PDF, bytes="")
    facts = _file_facts(f, {id(f): row})
    assert (facts.text_tokens, facts.native_tokens, facts.page_count, facts.has_text_layer) == (9_600, 15_000, 10, True)
    # A reference carries them too.
    ref = _file_facts(ReferenceAttachment(meta=row), {})
    assert (ref.text_tokens, ref.page_count) == (9_600, 10)


def test_a_row_without_current_facts_gives_none():
    row = _row("up-a", "a.pdf", PDF, text=1, pages=1)
    row.facts = {"version": 99}
    f = FileContent(filename="a.pdf", content_type=PDF, bytes="")
    assert _file_facts(f, {id(f): row}).text_tokens is None


def test_the_chat_ceiling_is_the_compaction_policys():
    assert routes._chat_ceiling(100_000) == 50_000
    assert routes._chat_ceiling(1_000_000) == 100_000


def test_over_budget_files_move_on_demand_in_attachment_order():
    """Haiku 5.5's budget is 25k: a prose 10-page PDF (~29k) leaves, a small docx stays text."""
    rows = [
        _row("up-a", "big.pdf", PDF, text=9_600, pages=10),
        _row("up-b", "notes.docx", DOCX, text=2_000),
        _row("up-c", "memo.docx", DOCX, text=60_000),
    ]
    attachments = _attachments(rows, token_profile())
    _carry_out_token_plan(attachments, _model(*HAIKU55))

    assert [f.filename for f in attachments.files_to_send] == ["notes.docx"]
    assert attachments.inline_indices == [1]
    refs = attachments.reference_documents
    assert [r.filename for r in refs] == ["big.pdf", "memo.docx"]
    assert attachments.reference_indices == [0, 2]
    assert all(r.over_budget and r.token_budget for r in refs)
    assert [e.reason for e in attachments.plan.entries] == [
        REASON_OVER_DOCUMENT_BUDGET, "within_budget", REASON_OVER_DOCUMENT_BUDGET]
    # The marker still names every file: nothing was dropped.
    assert attachments.marker_names == ["big.pdf", "notes.docx", "memo.docx"]


def test_the_same_pdf_fits_sonnet_4_6():
    rows = [_row("up-a", "big.pdf", PDF, text=9_600, pages=10)]
    attachments = _attachments(rows, token_profile())
    _carry_out_token_plan(attachments, _model(*SONNET46))
    assert [f.filename for f in attachments.files_to_send] == ["big.pdf"]
    assert attachments.plan.entries[0].mode == MODE_NATIVE
    assert attachments.reference_documents == []


def test_carrying_out_twice_moves_nothing_more():
    rows = [_row("up-a", "big.pdf", PDF, text=9_600, pages=10), _row("up-b", "n.docx", DOCX, text=500)]
    attachments = _attachments(rows, token_profile())
    _carry_out_token_plan(attachments, _model(*HAIKU55))
    once = (list(attachments.files_to_send), list(attachments.reference_documents))
    _carry_out_token_plan(attachments, _model(*HAIKU55))
    assert (attachments.files_to_send, attachments.reference_documents) == once


def test_the_legacy_profile_moves_nothing(monkeypatch):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", "true")
    rows = [_row("up-a", "big.pdf", PDF, text=90_000, pages=60), _row("up-c", "memo.docx", DOCX, text=60_000)]
    attachments = _attachments(rows, legacy_profile())
    _carry_out_token_plan(attachments, _model(*HAIKU55))
    assert [f.filename for f in attachments.files_to_send] == ["big.pdf", "memo.docx"]
    assert attachments.reference_documents == []
    assert all(e.mode != MODE_ON_DEMAND for e in attachments.plan.entries)


def test_the_note_names_the_budget_not_the_byte_limit():
    row = _row("up-a", "big.pdf", PDF, text=9_600, pages=10)
    note = routes._build_attachment_guidance(
        [], [], [], enabled_tools=["document_read"],
        references=[ReferenceAttachment(meta=row, over_budget=True, token_budget=True)],
    )
    assert "larger than this conversation keeps in context" in note
    assert "combined size limit" not in note
    assert "`document_read` with `upload_id` `up-a`" in note
