"""Prompt-cache classification of workflow prompt-step cost rows (workflow-designer.md §21.3).

Every prompt-step call has the same ``toolConfigHash`` (no tools) and ``systemPromptHash`` (one
constant), so a match on those two alone made any earlier prompt-step row a call's predecessor: a
second model's warm write, or a run over a new document, read as ``miss_avoidable`` with
``wastedUsd``, though no entry existed for it to read. The row's fingerprint is stateless, keyed
on the model and the document prefix, so only a call that wrote the same prefix counts.

These run the real ``meter_call`` (only the store is stubbed, to capture the row) and classify
the captured row with the real ``_derive_cache_observability`` against the earlier rows.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import workflow_prompt as wp
from apis.shared.files.whole_document import WholeDocument
from apis.shared.sessions import metadata as md

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
HAIKU = "us.anthropic.claude-haiku-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"
PRICING = {
    "inputPricePerMtok": 1.0,
    "outputPricePerMtok": 5.0,
    "cacheWritePricePerMtok": 1.25,
    "cacheReadPricePerMtok": 0.1,
    "currency": "USD",
    "snapshotAt": "2026-11-21T00:00:00Z",
}


def _doc(raw: bytes) -> WholeDocument:
    return WholeDocument(upload_id="f1", filename="proposal.pdf", fmt="pdf", pdf_bytes=raw, pages=4)


async def _record(model_id: str, raw: bytes, *, read: int = 0, write: int = 0, instruction: str = "Lens 1.") -> Any:
    """One prompt-step call's stored metadata, as ``_call_step`` meters it."""
    prefix = wp.document_blocks([_doc(raw)])
    messages = [{"role": "user", "content": wp.step_content(prefix, "", instruction, cache_point=True)}]
    store = AsyncMock(return_value={})
    with patch("apis.shared.sessions.metadata.store_message_metadata", store), patch(
        "apis.shared.costs.pricing_config.create_pricing_snapshot", AsyncMock(return_value=PRICING)
    ):
        await wp.meter_call(
            user_id="alice",
            session_id="src",
            model_id=model_id,
            usage={
                "inputTokens": 40,
                "outputTokens": 300,
                "totalTokens": 340 + read + write,
                "cacheReadInputTokens": read,
                "cacheWriteInputTokens": write,
            },
            metrics={},
            started=time.time(),
            ended=time.time(),
            fingerprint=wp._fingerprint(messages, wp.prefix_key(model_id, prefix)),
            attributes={"runId": "r1", "stepId": "s1"},
        )
    return store.await_args.kwargs["message_metadata"]


def _as_row(meta: Any, seconds_ago: float) -> Dict[str, Any]:
    row = meta.model_dump(by_alias=True, exclude_none=True)
    row["timestamp"] = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    return row


def _chat_row(seconds_ago: float) -> Dict[str, Any]:
    """A turn in the source conversation before the run: a big cached prefix of its own."""
    return {
        "timestamp": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "tokenUsage": {"inputTokens": 50, "outputTokens": 400, "cacheReadInputTokens": 108_000, "cacheWriteInputTokens": 900},
        "prefixFingerprints": {"toolConfigHash": "chat-tools", "systemPromptHash": "chat-system", "historyHash": "h"},
        "modelInfo": {"modelId": HAIKU, "provider": "bedrock"},
    }


class _FakeTable:
    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self._items = items

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        return {"Items": self._items}


def _classify(meta: Any, earlier_newest_first: List[Dict[str, Any]]) -> Dict[str, Any]:
    return md._derive_cache_observability(
        session_id="src", table=_FakeTable(earlier_newest_first), timestamp=NOW.isoformat(), message_metadata=meta
    )


PDF_A = b"%PDF-1.7 proposal A"
PDF_B = b"%PDF-1.7 proposal B"


async def test_the_first_warm_write_after_chat_turns_is_a_first_write():
    warm = await _record(HAIKU, PDF_A, write=10_080)

    result = _classify(warm, [_chat_row(30)])

    assert result["cacheStatus"] == "first_write"
    assert result["wastedUsd"] == 0


async def test_a_step_that_reads_the_warm_prefix_is_a_hit():
    warm = await _record(HAIKU, PDF_A, write=10_080)
    step = await _record(HAIKU, PDF_A, read=10_080, write=60, instruction="Lens 2.")

    assert _classify(step, [_as_row(warm, 5), _chat_row(40)])["cacheStatus"] == "hit"


async def test_a_second_models_warm_write_is_not_a_miss_of_the_first_models_entry():
    haiku_warm = await _record(HAIKU, PDF_A, write=10_080)
    haiku_step = await _record(HAIKU, PDF_A, read=10_080, write=60, instruction="Lens 2.")
    sonnet_warm = await _record(SONNET, PDF_A, write=10_080)

    result = _classify(sonnet_warm, [_as_row(haiku_step, 2), _as_row(haiku_warm, 8), _chat_row(60)])

    assert result["cacheStatus"] == "first_write"
    assert result["wastedUsd"] == 0


async def test_a_run_over_a_different_document_inside_the_ttl_is_not_a_miss():
    run_one = await _record(HAIKU, PDF_A, write=10_080)
    run_two = await _record(HAIKU, PDF_B, write=10_080)

    result = _classify(run_two, [_as_row(run_one, 90)])

    assert result["cacheStatus"] == "first_write"
    assert result["wastedUsd"] == 0


async def test_rewriting_the_same_prefix_on_the_same_model_inside_the_ttl_is_still_a_miss():
    """The key narrows the match; it does not hide real waste: the same prefix written twice."""
    warm = await _record(HAIKU, PDF_A, write=10_080)
    again = await _record(HAIKU, PDF_A, write=10_080, instruction="Lens 2.")

    result = _classify(again, [_as_row(warm, 20)])

    assert result["cacheStatus"] == "miss_avoidable"
    assert result["wastedUsd"] > 0


def test_the_key_names_the_model_and_the_document_bytes():
    prefix_a = wp.document_blocks([_doc(PDF_A)])

    assert wp.prefix_key(HAIKU, prefix_a) == wp.prefix_key(HAIKU, wp.document_blocks([_doc(PDF_A)]))
    assert wp.prefix_key(HAIKU, prefix_a) != wp.prefix_key(SONNET, prefix_a)
    assert wp.prefix_key(HAIKU, prefix_a) != wp.prefix_key(HAIKU, wp.document_blocks([_doc(PDF_B)]))


# --- Checklist calls (workflow-designer.md §22.3) --------------------------------------------------


async def _record_checklist(model_id: str, raw: bytes, *, read: int = 0, write: int = 0, field_set: str = "budget") -> Any:
    """One checklist call's stored metadata, as ``ChecklistBranch._run_set`` meters it."""
    from apis.inference_api.chat import workflow_checklist as wc
    from apis.shared.extraction import checklist as cl

    prefix = wp.document_blocks([_doc(raw)])
    instruction = f"Checklist: {field_set} (version 1)\n\nFields to find:\n- x (string): find it\n\n{cl.CALL_ONCE}"
    messages = [{"role": "user", "content": wp.step_content(prefix, "", instruction, cache_point=True)}]
    key = wp.prefix_key(model_id, prefix, system=cl.SYSTEM_PROMPT, tools=wc.TOOLS)
    store = AsyncMock(return_value={})
    with patch("apis.shared.sessions.metadata.store_message_metadata", store), patch(
        "apis.shared.costs.pricing_config.create_pricing_snapshot", AsyncMock(return_value=PRICING)
    ):
        await wp.meter_call(
            user_id="alice",
            session_id="src",
            model_id=model_id,
            usage={
                "inputTokens": 30,
                "outputTokens": 120,
                "totalTokens": 150 + read + write,
                "cacheReadInputTokens": read,
                "cacheWriteInputTokens": write,
            },
            metrics={},
            started=time.time(),
            ended=time.time(),
            fingerprint=wp._fingerprint(messages, key, tools=wc.TOOLS, system=cl.SYSTEM_PROMPT),
            attributes={"runId": "r1", "stepId": "s9", "fieldSet": field_set},
            call_kind=wc.CALL_KIND,
        )
    return store.await_args.kwargs["message_metadata"]


async def test_a_checklist_row_carries_its_kind_and_set():
    row = (await _record_checklist(HAIKU, PDF_A, write=10_400)).model_dump(by_alias=True, exclude_none=True)

    assert row["callKind"] == "workflow_checklist"
    assert row["fieldSet"] == "budget" and row["stepId"] == "s9"
    assert row["prefixFingerprints"]["toolConfigHash"] != wp._fingerprint([{"role": "user", "content": []}])["toolConfigHash"]


async def test_the_checklist_warm_write_after_a_prompt_steps_rows_is_a_first_write():
    """The tool list differs, so a prompt step's entry was never one a checklist call could read."""
    prompt_warm = await _record(HAIKU, PDF_A, write=10_080)
    prompt_step = await _record(HAIKU, PDF_A, read=10_080, write=60, instruction="Lens 2.")
    checklist_warm = await _record_checklist(HAIKU, PDF_A, write=10_400)

    result = _classify(checklist_warm, [_as_row(prompt_step, 3), _as_row(prompt_warm, 9), _chat_row(60)])

    assert result["cacheStatus"] == "first_write"
    assert result["wastedUsd"] == 0


async def test_a_second_set_on_the_same_model_is_a_hit():
    prompt_warm = await _record(HAIKU, PDF_A, write=10_080)
    checklist_warm = await _record_checklist(HAIKU, PDF_A, write=10_400)
    second = await _record_checklist(HAIKU, PDF_A, read=10_400, write=40, field_set="plans")

    result = _classify(second, [_as_row(checklist_warm, 4), _as_row(prompt_warm, 10)])

    assert result["cacheStatus"] == "hit"
    assert result["wastedUsd"] == 0


async def test_another_models_checklist_warm_write_is_not_a_miss():
    haiku = await _record_checklist(HAIKU, PDF_A, write=10_400)
    sonnet = await _record_checklist(SONNET, PDF_A, write=10_400)

    result = _classify(sonnet, [_as_row(haiku, 2)])

    assert result["cacheStatus"] == "first_write"
