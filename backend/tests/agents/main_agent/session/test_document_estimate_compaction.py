"""Compaction and offload estimate documents from row facts (unified-attachment-delivery §9.1, phase 2).

The case these reproduce ran on dev on 2026-11-07 (Haiku 5.5 pinned to a 100k
window: ceiling 50k, floor 12.5k, hard 70k). Turn 1 attached a 24-page, 134 KB
PDF; turn 2 read pages 1-8 and 9-16 back with ``document_read``. Measured:

* the static prefix (system + tools) was 6,546 tokens, the cache read of the
  call that applied the cut;
* the call that ended turn 2 billed 159,938, so the messages were 153,392;
* the cut kept turn 2 alone, and the next call billed 68,522: **61,976**
  retained.

Compaction logged ``retained≈22023`` (bytes/4 for every document, calibrated
against the whole input because the context breakdown had no ``messages``
partition while a document was inline). The row facts #1966 wrote for that
file are ``tokens.text`` 29,954, ``tokens.native`` 36,000, 24 pages.
"""

import copy
import io
import json
from datetime import datetime, timezone

import pypdfium2 as pdfium
import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import choose_checkpoint, estimate_message_tokens
from agents.main_agent.session.document_context import summarize_document_context
from agents.main_agent.session.document_estimate import DocumentIndex
from agents.main_agent.session.document_offload import age_document_slices, candidate_documents
from apis.shared.files.document_estimate import (
    DocumentTokenFacts,
    clear_session_documents,
    remember_session_documents,
)
from apis.shared.files.document_tokens import PDF_PAGE_TOKEN_ESTIMATE

from .conftest import TEST_SESSION_ID, TEST_USER_ID

UPLOAD_ID = "up-solicitation"
MEASURED_RETAINED = 68_522 - 6_546
MEASURED_HISTORY = 159_938 - 6_546


def _pdf(pages: int) -> bytes:
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(612, 792)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


PDF_24 = _pdf(24)
SLICE_8 = _pdf(8)

FACTS = DocumentTokenFacts(
    upload_id=UPLOAD_ID, filename="solicitation.pdf", size_bytes=len(PDF_24), file_format="pdf",
    text_tokens=29_954, native_tokens=36_000, pages=24,
)


class _Row:
    """A file row as the repository returns it."""

    upload_id = UPLOAD_ID
    user_id = TEST_USER_ID
    filename = "solicitation.pdf"
    size_bytes = len(PDF_24)
    file_format = "pdf"
    facts = {"version": 1, "tokens": {"text": 29_954, "native": 36_000}, "pages": 24}


@pytest.fixture(autouse=True)
def _clean():
    clear_session_documents()
    yield
    clear_session_documents()


def _attached(raw=PDF_24, name="solicitation_pdf"):
    return {"document": {"format": "pdf", "name": name, "source": {"bytes": raw}}}


def _slice_result(tool_use_id, start, end):
    payload = {
        "upload_id": UPLOAD_ID, "filename": "solicitation.pdf", "format": "pdf", "size_bytes": len(PDF_24),
        "mode": "pages", "page_count": 24, "pages": {"start": start, "end": end},
        "pages_returned": end - start + 1, "truncated_to_max_pages": False, "next_start": None,
        "page_numbering": f"The attached document contains original pages {start}-{end} only; "
                          f"its page k is original page {start} + k - 1.",
    }
    return {"toolResult": {"toolUseId": tool_use_id, "status": "success", "content": [
        {"json": payload},
        {"document": {"format": "pdf", "name": f"solicitation_p{start}-{end}_a1b2", "source": {"bytes": SLICE_8}}},
    ]}}


def _tool_use(tool_use_id, page_range):
    return {"toolUse": {"toolUseId": tool_use_id, "name": "document_read",
                        "input": {"upload_id": UPLOAD_ID, "page_range": page_range}}}


def _conversation():
    """The 2026-11-07 run, turn by turn (assistant text sized from its output tokens)."""
    return [
        {"role": "user", "content": [{"text": "Summarize this solicitation."}, _attached()]},
        {"role": "assistant", "content": [{"text": "s" * (1_516 * 4)}]},
        {"role": "user", "content": [{"text": "Read pages 1-16 and list every deadline with its page."}]},
        {"role": "assistant", "content": [{"text": "Reading pages 1-8."}, _tool_use("t1", "1-8")]},
        {"role": "user", "content": [_slice_result("t1", 1, 8)]},
        {"role": "assistant", "content": [{"text": "Now pages 9-16."}, _tool_use("t2", "9-16")]},
        {"role": "user", "content": [_slice_result("t2", 9, 16)]},
        {"role": "assistant", "content": [{"text": "d" * (1_320 * 4)}]},
    ]


# ---------------------------------------------------------------------------
# Per block kind
# ---------------------------------------------------------------------------

class TestBlockEstimates:
    def test_native_pdf_reads_its_row(self):
        index = DocumentIndex([FACTS])
        assert index.block_tokens(_attached()["document"]) == 29_954 + 36_000

    def test_the_block_is_matched_by_sanitized_name_with_a_duplicate_suffix(self):
        index = DocumentIndex([FACTS])
        assert index.block_tokens(_attached(name="solicitation_pdf_2")["document"]) == 65_954

    def test_a_slice_is_its_pages_share_of_the_row(self):
        index = DocumentIndex([FACTS])
        message = {"role": "user", "content": [_slice_result("t1", 1, 8)]}
        estimate = estimate_message_tokens(message, index)
        assert round(65_954 * 8 / 24) < estimate < round(65_954 * 8 / 24) + 400  # plus the JSON payload

    def test_a_slice_without_facts_is_its_own_pages(self):
        """No row facts: ``estimate_document_tokens`` on the slice's bytes,
        which parses its 8 pages, never bytes/4."""
        message = {"role": "user", "content": [_slice_result("t1", 1, 8)]}
        estimate = estimate_message_tokens(message)
        assert 8 * PDF_PAGE_TOKEN_ESTIMATE <= estimate < 8 * PDF_PAGE_TOKEN_ESTIMATE + 400
        assert estimate > len(SLICE_8)  # bytes/4 would be ~350

    def test_a_legacy_row_falls_back_to_the_page_estimate(self):
        """A row written before #1966 has no facts, so nothing is indexed."""
        message = {"role": "user", "content": [_attached()]}
        assert estimate_message_tokens(message) == 24 * PDF_PAGE_TOKEN_ESTIMATE + 4

    def test_a_text_format_block_is_its_text(self):
        docx = DocumentTokenFacts(
            upload_id="up-memo", filename="memo.docx", size_bytes=30_000, file_format="docx",
            text_tokens=40_000, native_tokens=7_500,
        )
        block = {"document": {"format": "docx", "name": "memo_docx", "source": {"bytes": b"PK" * 15_000}}}
        assert DocumentIndex([docx]).block_tokens(block["document"]) == 40_000
        assert DocumentIndex().block_tokens(block["document"]) == 7_500  # bytes/4, the documented fallback

    def test_the_cost_row_footprint_reads_the_same_numbers(self):
        footprint = summarize_document_context(_conversation(), DocumentIndex([FACTS]))
        assert footprint["documentTokens"] == 65_954
        assert footprint["documentSliceTokens"] == 2 * round(65_954 * 8 / 24)

    def test_an_unknown_index_object_is_ignored(self):
        """The coordinator passes whatever the session manager answers; a mock is not an index."""
        assert summarize_document_context(_conversation(), object())["documentTokens"] == 36_000

    def test_estimates_never_change_a_message(self):
        messages = _conversation()
        before = json.dumps(messages, default=repr, sort_keys=True)
        index = DocumentIndex([FACTS])
        for message in messages:
            estimate_message_tokens(message, index)
        assert json.dumps(messages, default=repr, sort_keys=True) == before


# ---------------------------------------------------------------------------
# The 2026-11-07 cut
# ---------------------------------------------------------------------------

class TestTwentyFourPagePdfCut:
    def _cut(self, index, history):
        return choose_checkpoint(
            _conversation(), [0, 2], 3, floor_tokens=12_500, history_tokens=history,
            tail_bound_tokens=50_000, documents=index,
        )

    def test_retained_estimate_is_within_15_percent_of_measured(self):
        cut, retained = self._cut(DocumentIndex([FACTS]), MEASURED_HISTORY)
        assert cut == 2  # the newest turn alone, as on dev
        assert abs(retained - MEASURED_RETAINED) <= 0.15 * MEASURED_RETAINED, retained

    def test_a_legacy_row_still_lands_within_15_percent(self):
        """Without facts every PDF is pages x the page estimate, attachment and
        slice alike, so their ratio, which is what calibration keeps, holds."""
        _, retained = self._cut(None, MEASURED_HISTORY)
        assert abs(retained - MEASURED_RETAINED) <= 0.15 * MEASURED_RETAINED, retained


class TestUpdateAfterTurn:
    def _manager(self, make_session_manager):
        store = {}
        cfg = CompactionConfig(enabled=True, protected_turns=3, summary_token_budget=200)
        mgr = make_session_manager(compaction_config=cfg)
        mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

        def _save(state, record_event=False):
            state.updated_at = datetime.now(timezone.utc).isoformat()
            store["compaction"] = state.to_dict()

        mgr._save_compaction_state = _save
        mgr._retrieve_session_summaries = lambda: ["LTM summary"]
        mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
        return mgr, store

    @pytest.mark.asyncio
    async def test_the_turn_registered_facts_reach_the_cut(self, make_session_manager):
        mgr, _ = self._manager(make_session_manager)
        # What the route does when it resolves the turn's uploads.
        remember_session_documents(TEST_USER_ID, TEST_SESSION_ID, [_Row()])
        live = _conversation()
        before = json.dumps(live, default=repr, sort_keys=True)

        await mgr.update_after_turn(
            159_938, current_messages=live, context_window=100_000, history_tokens=MEASURED_HISTORY
        )

        policy = mgr.compaction_state.policy
        assert (policy["ceiling"], policy["floor"], policy["hardCeiling"]) == (50_000, 12_500, 70_000)
        retained = policy["retainedTokensEstimate"]
        assert abs(retained - MEASURED_RETAINED) <= 0.15 * MEASURED_RETAINED, retained
        assert json.dumps(live, default=repr, sort_keys=True) == before, "estimates move cuts, never bytes"

    @pytest.mark.asyncio
    async def test_another_users_facts_are_not_read(self, make_session_manager):
        mgr, _ = self._manager(make_session_manager)
        remember_session_documents("someone-else", TEST_SESSION_ID, [type("R", (_Row,), {"user_id": "someone-else"})()])
        assert not mgr.document_index()

    @pytest.mark.asyncio
    async def test_a_miss_is_filled_after_the_turn_from_the_session_rows(self, make_session_manager, monkeypatch):
        """Facts that landed after the turn read the row: one Query, after the
        turn, only when a cut is being chosen."""
        mgr, _ = self._manager(make_session_manager)
        reads = []

        def _load(session_id, user_id):
            reads.append((session_id, user_id))
            return [_Row()]

        monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", "files")
        monkeypatch.setattr("agents.main_agent.session.document_rehydration.load_session_documents", _load)
        await mgr.update_after_turn(
            159_938, current_messages=_conversation(), context_window=100_000, history_tokens=MEASURED_HISTORY
        )
        assert reads == [(TEST_SESSION_ID, TEST_USER_ID)]
        retained = mgr.compaction_state.policy["retainedTokensEstimate"]
        assert abs(retained - MEASURED_RETAINED) <= 0.15 * MEASURED_RETAINED

    @pytest.mark.asyncio
    async def test_no_read_under_the_ceiling(self, make_session_manager, monkeypatch):
        mgr, _ = self._manager(make_session_manager)
        monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", "files")
        monkeypatch.setattr(
            "agents.main_agent.session.document_rehydration.load_session_documents",
            lambda *a: pytest.fail("no read when no cut is chosen"),
        )
        await mgr.update_after_turn(40_000, current_messages=_conversation(), context_window=100_000)

    @pytest.mark.asyncio
    async def test_the_anchor_and_checkpoint_stay_monotonic(self, make_session_manager):
        mgr, store = self._manager(make_session_manager)
        remember_session_documents(TEST_USER_ID, TEST_SESSION_ID, [_Row()])
        live = _conversation()
        await mgr.update_after_turn(159_938, current_messages=live, context_window=100_000, history_tokens=MEASURED_HISTORY)
        first = CompactionState.from_dict(store["compaction"])
        await mgr.update_after_turn(159_938, current_messages=live, context_window=100_000, history_tokens=MEASURED_HISTORY)
        second = CompactionState.from_dict(store["compaction"])
        assert second.checkpoint >= first.checkpoint
        assert second.truncation_anchor >= first.truncation_anchor
        assert (second.pending_checkpoint or 0) >= (first.pending_checkpoint or 0)


class TestOffloadReadsFacts:
    def test_candidate_weight_is_the_rows(self):
        messages = copy.deepcopy(_conversation())
        cands = candidate_documents(messages, set(), min_tokens=5_000, documents=DocumentIndex([FACTS]))
        assert [c.tokens for c in cands] == [65_954]

    def test_a_small_pdf_with_dense_text_now_clears_the_floor(self):
        """3 pages are 4,500 by the page estimate alone, under the 5,000 floor;
        with their text layer they are worth evicting."""
        small = DocumentTokenFacts(
            upload_id="up-s", filename="brief.pdf", size_bytes=len(_pdf(3)), file_format="pdf",
            text_tokens=3_000, native_tokens=4_500, pages=3,
        )
        messages = [{"role": "user", "content": [_attached(_pdf(3), name="brief_pdf")]}]
        assert candidate_documents(messages, set(), min_tokens=5_000) == []
        assert [c.tokens for c in candidate_documents(messages, set(), min_tokens=5_000, documents=DocumentIndex([small]))] == [7_500]

    def test_aged_slices_are_counted_from_the_row(self):
        messages = _conversation() + [
            {"role": "user", "content": [{"text": "turn 3"}]},
            {"role": "assistant", "content": [{"text": "ok"}]},
            {"role": "user", "content": [{"text": "turn 4"}]},
        ]
        aged, tokens = age_document_slices(messages, max_turns=2, documents=DocumentIndex([FACTS]))
        assert aged == 2 and tokens == 2 * round(65_954 * 8 / 24)
