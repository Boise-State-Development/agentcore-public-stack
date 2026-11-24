"""The ``Script version:`` line the rail reads (skill-code-sandbox §8.11).

``script-version.fixture.json`` holds hashes and the line ``run_code`` writes for
each; the SPA's ``skill-script-call.spec.ts`` parses the same file, so a change on
either side fails a test until both agree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apis.shared.skills import script_trust as st

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "ai.client"
    / "src"
    / "app"
    / "session"
    / "components"
    / "message-list"
    / "components"
    / "tool-rail"
    / "script-version.fixture.json"
)


def _cases():
    if not FIXTURE.exists():
        pytest.skip("frontend package not in this checkout")
    return json.loads(FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_server_writes_the_line_the_rail_parses(case):
    assert st.script_version_line(case["scriptsHash"]) == case["line"]
    if case["line"]:
        assert case["line"] in case["resultText"].splitlines()
        assert case["version"] == st.short_hash(case["scriptsHash"])
