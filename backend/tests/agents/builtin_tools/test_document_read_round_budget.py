"""A tool round's page reads share one byte budget (``document_read_tool``).

The failure this pins: five parallel ``document_read`` page reads of ~3.3 MB
each landed in ONE tool-result message — Strands writes a round's results
together — and that message is one AgentCore Memory event. Base64 made it
~22 MB against a 10 MB quota, ``CreateEvent`` returned 413, and Strands
force-stopped the turn. The tool now takes each call's share in turn under a
per-round lock, so a round's PDFs can never add up past the budget, and the
message stays under the quota as the SDK actually encodes it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.builtin_tools import document_read_tool as drt
from apis.shared.files.document_read import DocumentReadResult

#: About what one of the incident's reads returned (16.5 MB over five calls).
SLICE_BYTES = 3_300_000
#: AgentCore Memory's event quota, which the blob payload is held to.
EVENT_QUOTA = 10_000_000


def _round(*ids: str) -> list:
    """The assistant message that asked for this round's tool calls."""
    return [{"role": "assistant", "content": [
        {"toolUse": {"toolUseId": i, "name": "document_read", "input": {}}} for i in ids
    ]}]


def _context(tool_use_id: str, messages: list):
    return SimpleNamespace(tool_use={"toolUseId": tool_use_id}, agent=SimpleNamespace(messages=messages))


def _fake_read(slice_bytes: int = SLICE_BYTES, caps: list | None = None):
    """``read_document`` that honors ``max_document_bytes`` the way the real
    one does: the slice if it fits, otherwise the text layer."""

    async def read(user_id, session_id, upload_id, *, max_document_bytes=None, **kwargs):
        if caps is not None:
            caps.append(max_document_bytes)
        await asyncio.sleep(0)  # let the round's other calls interleave
        if max_document_bytes is not None and slice_bytes > max_document_bytes:
            return DocumentReadResult(
                mode="pages_text", payload={"page_images_omitted": "byte_budget", "page_texts": []}, format="pdf"
            )
        block = {"document": {"format": "pdf", "name": f"{upload_id} p1-8 abc123",
                              "source": {"bytes": b"\x00" * slice_bytes}}}
        return DocumentReadResult(
            mode="pages", payload={"pages_returned": 8}, document_block=block,
            pages_returned=8, bytes_returned=slice_bytes, format="pdf",
        )

    return read


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


async def _run_round(tool, ids, messages, **kwargs):
    return await asyncio.gather(*(
        _call(tool, upload_id=f"up-{i}", page_range="1-8", tool_context=_context(i, messages), **kwargs)
        for i in ids
    ))


def _documents(results) -> list:
    return [b for r in results for b in r["content"] if "document" in b]


def _memory_blob_bytes(results, ids) -> int:
    """The round's user message, encoded exactly as the AgentCore SDK writes
    an oversized message to ``CreateEvent`` (a ``blob`` payload)."""
    from bedrock_agentcore.memory.integrations.strands.bedrock_converter import AgentCoreMemoryConverter
    from strands.types.session import SessionMessage

    message = {"role": "user", "content": [
        {"toolResult": {"toolUseId": i, "status": r["status"], "content": r["content"]}}
        for i, r in zip(ids, results)
    ]}
    payload = AgentCoreMemoryConverter.message_to_payload(SessionMessage.from_message(message, 0))
    return len(json.dumps(payload[0]))


@pytest.mark.asyncio
class TestRoundBudget:
    IDS = ("t1", "t2", "t3", "t4", "t5")

    async def test_the_incidents_round_now_fits_one_memory_event(self, monkeypatch):
        monkeypatch.setattr(drt, "read_document", _fake_read())
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        results = await _run_round(tool, self.IDS, _round(*self.IDS))

        docs = _documents(results)
        assert sum(len(d["document"]["source"]["bytes"]) for d in docs) <= 7_000_000
        assert len(docs) == 2  # two 3.3 MB slices fit; the other three get text
        assert all(r["status"] == "success" for r in results)
        assert _memory_blob_bytes(results, self.IDS) < EVENT_QUOTA

    async def test_without_the_budget_the_same_round_is_over_the_quota(self, monkeypatch):
        # The control: proves the assertion above measures the real failure.
        monkeypatch.setattr(drt, "read_document", _fake_read())
        monkeypatch.setattr(drt, "DOCUMENT_READ_MAX_BATCH_BYTES", 0)
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        results = await _run_round(tool, self.IDS, _round(*self.IDS))

        assert len(_documents(results)) == 5
        assert _memory_blob_bytes(results, self.IDS) > EVENT_QUOTA

    async def test_each_call_gets_what_the_round_has_left(self, monkeypatch):
        caps: list = []
        monkeypatch.setattr(drt, "read_document", _fake_read(caps=caps))
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        await _run_round(tool, self.IDS, _round(*self.IDS))

        assert caps == [7_000_000, 3_700_000, 400_000, 400_000, 400_000]

    async def test_a_new_round_starts_with_the_whole_budget(self, monkeypatch):
        caps: list = []
        monkeypatch.setattr(drt, "read_document", _fake_read(caps=caps))
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        await _run_round(tool, ("a1", "a2", "a3"), _round("a1", "a2", "a3"))
        caps.clear()
        await _run_round(tool, ("b1",), _round("b1"))

        assert caps == [7_000_000]

    async def test_a_call_with_no_context_gets_a_budget_of_its_own(self, monkeypatch):
        caps: list = []
        monkeypatch.setattr(drt, "read_document", _fake_read(caps=caps))
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        await _call(tool, upload_id="up-1", page_range="1-8")
        await _call(tool, upload_id="up-2", page_range="1-8")

        assert caps == [7_000_000, 7_000_000]

    @pytest.mark.parametrize("kwargs", [{"pattern": "termination"}, {}])
    async def test_reads_that_return_no_pdf_take_no_share(self, monkeypatch, kwargs):
        caps: list = []
        monkeypatch.setattr(drt, "read_document", _fake_read(caps=caps))
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        await _call(tool, upload_id="up-1", tool_context=_context("t1", _round("t1")), **kwargs)

        assert caps == [None]

    async def test_the_searchable_tool_shares_the_budget_too(self, monkeypatch):
        monkeypatch.setattr(drt, "read_document", _fake_read())
        tool = drt.make_document_read_tool("s1", "u1", searchable=True)

        results = await _run_round(tool, self.IDS, _round(*self.IDS))

        assert len(_documents(results)) == 2

    async def test_the_budget_is_recorded_when_it_shapes_a_read(self, monkeypatch):
        records = []
        monkeypatch.setattr(drt, "read_document", _fake_read())
        monkeypatch.setattr(
            "apis.shared.observability.emf.emit_emf_metrics",
            lambda namespace, metrics, properties, units: records.append((metrics, properties)),
        )
        tool = drt.make_document_read_tool("s1", "u1", searchable=False)

        await _run_round(tool, self.IDS, _round(*self.IDS))

        limited = [p for m, p in records if m.get("DocumentReadByteBudgetLimited") == 1]
        assert [p["byte_budget"] for p in limited] == ["text", "text", "text"]


def test_the_context_parameter_never_reaches_the_tool_spec():
    tool = drt.make_document_read_tool("s1", "u1", searchable=False)
    assert "tool_context" not in tool.tool_spec["inputSchema"]["json"]["properties"]
