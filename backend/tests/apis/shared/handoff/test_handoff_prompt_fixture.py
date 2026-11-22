"""Prompt preview parity (handoffs spec §10): the SPA's fixture still matches the server's wrapper.

``handoff-prompt.fixture.json`` holds inputs and the outputs ``apis.shared.handoff.context`` produced
for them. The SPA's ``handoff-prompt.spec.ts`` checks its TypeScript copy against the same file, so a
change on either side fails a test until the fixture is regenerated from Python and both agree.
"""

from __future__ import annotations

import json
from pathlib import Path

from types import SimpleNamespace

import pytest

from apis.shared.handoff.context import build_handoff_message, context_hash, format_file_size, wrap_handoff_context

FIXTURE = (
    Path(__file__).resolve().parents[5]
    / "frontend"
    / "ai.client"
    / "src"
    / "app"
    / "session"
    / "services"
    / "handoff"
    / "handoff-prompt.fixture.json"
)


def _cases():
    if not FIXTURE.exists():
        pytest.skip("frontend package not in this checkout")
    return json.loads(FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_server_still_builds_what_the_fixture_says(case):
    block = wrap_handoff_context(case["card"], case["title"], _files(case))

    assert block == case["contextBlock"]
    assert build_handoff_message(block, case["request"]) == case["prompt"]
    assert (context_hash(case["card"]) if case["card"] else None) == case["hash"]


def _delivery_cases():
    if not FIXTURE.exists():
        pytest.skip("frontend package not in this checkout")
    return json.loads(FIXTURE.read_text())["deliveryCases"]


@pytest.mark.parametrize("case", _delivery_cases(), ids=lambda c: c["name"])
def test_the_server_still_renders_the_delivery_blocks_the_fixture_says(case):
    from apis.shared.handoff.context import render_delivery_block
    from apis.shared.handoff.delivery import DeliveryArtifact

    if "parts" in case:
        # A run result (workflow-runs §7): one block per part, joined, as ``DeliveryTurn`` renders it.
        from apis.shared.handoff.context import DeliveryPartBlock, render_delivery_parts

        parts = [
            DeliveryPartBlock(
                text=p["text"],
                agent_name=p["agentName"],
                from_title=p["fromTitle"],
                status=p["status"],
                artifacts=[DeliveryArtifact.model_validate(a) for a in p["artifacts"]],
                files=_files(p),
                prompt=p.get("prompt", False),
            )
            for p in case["parts"]
        ]
        assert render_delivery_parts(parts) == case["block"]
        return

    artifacts = [DeliveryArtifact.model_validate(a) for a in case["artifacts"]]

    assert render_delivery_block(case["text"], case["fromTitle"], artifacts, _files(case)) == case["block"]


def test_the_fixture_pins_a_run_result_with_every_part_status():
    statuses = {p["status"] for c in _delivery_cases() if "parts" in c for p in c["parts"]}

    assert statuses == {"done", "stopped", "needs_input", "running"}


def _files(case):
    return [
        SimpleNamespace(
            upload_id=f["uploadId"], filename=f["filename"], mime_type=f["mimeType"], size_bytes=f["sizeBytes"]
        )
        for f in case.get("files", [])
    ]


def _fixture(key):
    if not FIXTURE.exists():
        pytest.skip("frontend package not in this checkout")
    return json.loads(FIXTURE.read_text())[key]


@pytest.mark.parametrize("case", _fixture("referenceIdCases"), ids=lambda c: c["id"])
def test_the_server_still_names_reference_rows_as_the_fixture_says(case):
    from apis.shared.files.linked_files import reference_upload_id

    assert reference_upload_id(case["original"], case["link"]) == case["id"]


@pytest.mark.parametrize("case", _fixture("fileSizeCases"), ids=lambda c: str(c["sizeBytes"]))
def test_the_server_still_labels_file_sizes_as_the_fixture_says(case):
    assert format_file_size(case["sizeBytes"]) == case["label"]
