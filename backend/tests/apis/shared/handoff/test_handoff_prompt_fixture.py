"""Prompt preview parity (handoffs spec §10): the SPA's fixture still matches the server's wrapper.

``handoff-prompt.fixture.json`` holds inputs and the outputs ``apis.shared.handoff.context`` produced
for them. The SPA's ``handoff-prompt.spec.ts`` checks its TypeScript copy against the same file, so a
change on either side fails a test until the fixture is regenerated from Python and both agree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apis.shared.handoff.context import build_handoff_message, context_hash, wrap_handoff_context

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
    block = wrap_handoff_context(case["card"], case["title"])

    assert block == case["contextBlock"]
    assert build_handoff_message(block, case["request"]) == case["prompt"]
    assert (context_hash(case["card"]) if case["card"] else None) == case["hash"]
