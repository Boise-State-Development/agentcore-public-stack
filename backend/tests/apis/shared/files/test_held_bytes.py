"""``held_native_bytes``: what a warm agent's history re-sends (§4.4.1)."""

from __future__ import annotations

import base64
import time

from apis.shared.files.attachment_handle import HANDLE_KEY
from apis.shared.files.held_bytes import held_native_bytes


def _doc(raw, fmt="pdf"):
    return {"document": {"format": fmt, "name": "a", "source": {"bytes": raw}}}


def _img(raw):
    return {"image": {"format": "png", "source": {"bytes": raw}}}


def test_counts_documents_and_images_in_every_message():
    messages = [
        {"role": "user", "content": [{"text": "hi"}, _doc(b"x" * 1_000), _img(b"y" * 500)]},
        {"role": "assistant", "content": [{"text": "ok"}]},
        {"role": "user", "content": [_doc(b"z" * 250, fmt="docx")]},
    ]
    assert held_native_bytes(messages) == 1_750


def test_counts_document_read_slices_in_tool_results():
    slice_block = {"toolResult": {"toolUseId": "t", "status": "success", "content": [{"text": "p1"}, _doc(b"s" * 300)]}}
    assert held_native_bytes([{"role": "user", "content": [slice_block]}]) == 300


def test_a_base64_string_counts_its_decoded_length():
    encoded = base64.b64encode(b"q" * 3_000).decode("ascii")
    assert held_native_bytes([{"role": "user", "content": [_doc(encoded)]}]) == 3_000


def test_a_handle_or_a_digest_counts_nothing():
    handle = {"document": {"format": "pdf", "name": "a", "source": {HANDLE_KEY: {"sizeBytes": 9_000_000}}}}
    digest = {"text": "<document_digest>…</document_digest>"}
    assert held_native_bytes([{"role": "user", "content": [handle, digest]}]) == 0


def test_odd_shapes_never_raise():
    assert held_native_bytes(None) == 0
    assert held_native_bytes([None, "x", {"content": "text"}, {"content": [None, 3, {"image": None}]}]) == 0


def test_a_long_conversation_counts_in_well_under_a_millisecond():
    """The TTFT check at realistic scale: 400 messages, 20 PDFs, 30 tool results."""
    pdf = b"%PDF" + b"0" * 3_000_000
    messages = []
    for i in range(200):
        user = [{"text": "question " * 40}]
        if i % 10 == 0:
            user.append(_doc(pdf))
        if i % 7 == 0:
            user.append({"toolResult": {"toolUseId": f"t{i}", "status": "success",
                                        "content": [{"text": "result " * 200}]}})
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": [{"text": "answer " * 80}, {"toolUse": {"name": "x"}}]})
    started = time.perf_counter()
    for _ in range(100):
        total = held_native_bytes(messages)
    per_call_ms = (time.perf_counter() - started) * 1000 / 100
    assert total == 20 * len(pdf)
    assert per_call_ms < 1.0, per_call_ms
