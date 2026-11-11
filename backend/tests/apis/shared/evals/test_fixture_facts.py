"""Eval fixtures carry the facts a real upload gets (unified-attachment-delivery §4.1).

``register_upload`` writes rows straight to the table, so preparation never runs
for them. Without facts every fixture planned as ``facts_pending`` and the
``token`` profile's budget was never exercised by a direct-upload run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apis.shared.evals.fixtures import _document_facts
from apis.shared.files.file_facts import FACTS_VERSION, compute_document_facts

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from eval_fixtures import delivery as d  # noqa: E402


@pytest.mark.parametrize("name,mime,fmt", [
    ("delivery_memo_30k.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    ("delivery_prose_5k.md", "text/markdown", "md"),
    ("delivery_report_30k.pdf", "application/pdf", "pdf"),
])
def test_a_document_fixture_gets_the_preparation_facts(name, mime, fmt):
    data = d.GENERATED[name]()
    facts = _document_facts(name, mime, data)
    assert facts == compute_document_facts(fmt, data)
    assert facts["version"] == FACTS_VERSION


@pytest.mark.parametrize("name,mime", [("delivery_permit_card.png", "image/png"), ("delivery_ledger_30k.xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
def test_anything_else_gets_none(name, mime):
    assert _document_facts(name, mime, b"\x89PNG") is None


def test_a_file_that_will_not_parse_gets_none():
    assert _document_facts("broken.pdf", "application/pdf", b"not a pdf") is None


# --- the search arm (phase 5) ---


def test_a_search_fixture_is_digested_and_sent_to_the_index(monkeypatch):
    """A forced ``search`` row gets the on_demand digest and one ``index`` message
    to the deployed prepare queue, which ingests it (``indexed_by_budget``)."""
    import asyncio
    import json

    from apis.shared.evals import fixtures as fx

    calls = {"digest": [], "sent": []}

    async def digest(*args):
        calls["digest"].append(args[1])

    class Sqs:
        def send_message(self, QueueUrl, MessageBody):
            calls["sent"].append((QueueUrl, json.loads(MessageBody)))

    monkeypatch.setattr(fx, "_store_digest", digest)
    monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "https://sqs/q")
    monkeypatch.setattr("boto3.client", lambda name, *a, **k: Sqs())
    asyncio.run(fx._enqueue_index("u1", "up-1"))
    assert calls["sent"] == [("https://sqs/q", {"action": "index", "user_id": "u1", "upload_id": "up-1"})]


def test_the_search_arm_needs_the_prepare_queue(monkeypatch):
    import asyncio

    from apis.shared.evals import fixtures as fx

    monkeypatch.delenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", raising=False)
    with pytest.raises(fx.FixtureError, match="prepare queue"):
        asyncio.run(fx._enqueue_index("u1", "up-1"))


@pytest.mark.parametrize("state,settled", [
    ({"preparation": None, "kbIndex": None, "textIndexed": False}, False),
    ({"preparation": None, "kbIndex": "indexing", "textIndexed": False}, False),
    ({"preparation": None, "kbIndex": "indexing", "textIndexed": True}, True),
    ({"preparation": None, "kbIndex": "indexed", "textIndexed": False}, True),
    ({"preparation": None, "kbIndex": "skipped_cap", "textIndexed": False}, True),
    ({"preparation": "missing", "kbIndex": None, "textIndexed": False}, True),
])
def test_searchable_settled_needs_no_preparation(state, settled):
    from apis.shared.evals.fixtures import searchable_settled

    assert searchable_settled(state) is settled
