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
