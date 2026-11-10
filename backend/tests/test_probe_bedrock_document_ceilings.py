"""The document-ceilings probe's request building and readout, with no Bedrock call."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import probe_bedrock_document_ceilings as probe  # noqa: E402


class _Client:
    """Accepts up to ``max_pages`` pages and ``max_wire`` bytes, like Bedrock on a 1M model."""

    def __init__(self, max_pages=600, max_wire=32 * probe.MB):
        self.max_pages, self.max_wire, self.calls = max_pages, max_wire, []

    def converse(self, *, modelId, messages, inferenceConfig):
        docs = [b["document"]["source"]["bytes"] for b in messages[0]["content"] if "document" in b]
        self.calls.append(len(docs))
        pages = sum(d.count(b"/Type /Page ") for d in docs)
        if probe.wire_bytes([len(d) for d in docs]) > self.max_wire:
            raise _Refused("Input is too long for requested model.")
        if pages > self.max_pages:
            raise _Refused(f"A maximum of {self.max_pages} PDF pages may be provided.")
        return {"usage": {"inputTokens": 1560 * pages}}


class _Refused(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.response = {"Error": {"Code": "ValidationException", "Message": message}}


def test_a_page_pdf_has_its_pages_and_a_padded_one_its_size():
    assert probe.pages_pdf(7).count(b"/Type /Page ") == 7
    raw = probe.raw_for_wire(5.0)
    assert abs(len(probe.padded_pdf(raw)) - raw) < 0.01 * raw
    assert abs(probe.wire_bytes([raw]) - 5 * probe.MB) < 2_000


def test_pages_bisect_to_the_ceiling():
    prober = probe.Prober(_Client(max_pages=100), max_input_tokens=10**7, dry_run=False)
    readout = probe.ModelReadout("m", probe.probe_pages(prober, "m", [50, 600], bisect_steps=12))
    assert readout.ceiling("pages", lambda a: a.pages) == {"maxAccepted": 100, "minRefused": 101}


def test_a_page_count_projected_over_the_budget_is_skipped_not_sent():
    client = _Client()
    prober = probe.Prober(client, max_input_tokens=200_000, dry_run=False)
    attempts = probe.probe_pages(prober, "m", [100, 600], bisect_steps=0)
    (skipped,) = [a for a in attempts if a.pages == 600]
    assert not skipped.sent and "projected" in skipped.skipped and len(client.calls) == 2


def test_payload_refusal_is_recorded_verbatim_and_the_split_parts_stay_small():
    prober = probe.Prober(_Client(), max_input_tokens=10**7, dry_run=False)
    attempts = probe.probe_payload(prober, "m", [31.0, 33.0])
    refused = [a for a in attempts if a.accepted is False]
    assert {a.probe for a in refused} == {"payload", "payload_split"}
    assert all(a.error == "Input is too long for requested model." for a in refused)
    split = [a for a in attempts if a.probe == "payload_split"]
    assert all(a.raw_bytes / a.documents < probe.SPLIT_PART_MB * probe.MB for a in split)
    assert "smallest refused 33.0 MB" in probe.format_report([probe.ModelReadout("m", attempts)])


def test_dry_run_sends_nothing():
    client = _Client()
    probe.probe_payload(probe.Prober(client, max_input_tokens=1, dry_run=True), "m", [19.0])
    assert client.calls == []
