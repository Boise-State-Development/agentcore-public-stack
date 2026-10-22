"""The bootstrap seeder and the Python tool catalog must describe the same tools.

There are two independent lists of tools in this repo and nothing syncs them:

  * ``TOOL_CATALOG`` in ``agents/main_agent/tools/tool_catalog.py`` — metadata the
    agent side reads.
  * ``DEFAULT_TOOLS`` in ``scripts/seed_bootstrap_data.py`` — the ONLY writer of
    the ``TOOL#<id>`` rows in DynamoDB that RBAC grants and the tool picker read.

A tool in the catalog but not the seeder has no row on a fresh install, so no role
can be granted it and no user can enable it — silently, because an absent row reads
as "not in the catalog", not as an error. That is exactly what happened to
``list_spreadsheets`` / ``analyze_spreadsheet``: they were catalogued but never
seeded, and the live environments only have them because the rows were created by
hand. A fork got no spreadsheet analysis at all.

The reverse direction is legitimate and is NOT asserted: several seeded ids
(``create_artifact``, the document-authoring tools, ``workspace_files``) are
context-bound tools built per request, so they have a catalog ROW without a
``TOOL_CATALOG`` entry. Only catalog-without-row is a defect.

Deliberately excluded: ids in ``DOCUMENT_TOOL_IDS``. ``document_read`` is gated on
the session having a readable attachment rather than on ``enabled_tools``, so it
has no catalog entry, no RBAC grant and no picker toggle by design
(``apis/shared/tools/injected.py``).
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from agents.main_agent.tools.tool_catalog import TOOL_CATALOG  # noqa: E402
from apis.shared.tools.injected import DOCUMENT_TOOL_IDS  # noqa: E402

SEED_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "seed_bootstrap_data.py"
)


def _seeded_tool_ids() -> set[str]:
    """Every ``toolId`` the seeder writes, read from source.

    Parsed rather than imported: the module builds boto3 sessions at import in
    some paths, and the list is a plain literal, so a regex is both cheaper and
    less likely to break for an unrelated reason.
    """
    with open(SEED_SCRIPT, encoding="utf-8") as fh:
        source = fh.read()

    return set(re.findall(r'"toolId":\s*"([A-Za-z0-9_]+)"', source))


def test_every_catalogued_tool_has_a_seed_row() -> None:
    catalogued = set(TOOL_CATALOG.keys()) - set(DOCUMENT_TOOL_IDS)
    missing = catalogued - _seeded_tool_ids()

    assert not missing, (
        f"Tools in TOOL_CATALOG with no row in seed_bootstrap_data.py: "
        f"{sorted(missing)}. A fresh deployment would list them nowhere, so no "
        f"role could be granted them. Add a DEFAULT_TOOLS entry (and if the tool "
        f"is context-bound, check apis/shared/tools/injected.py first — some ids "
        f"are deliberately ungated and belong in DOCUMENT_TOOL_IDS instead)."
    )


def test_the_seeder_parses_at_all() -> None:
    """Guard the regex above: if it stops matching, the test above passes vacuously."""
    seeded = _seeded_tool_ids()

    assert len(seeded) >= 10, (
        f"Only {len(seeded)} toolIds parsed out of the seeder — the literal's "
        f"shape probably changed and the parity check above is now vacuous."
    )
    assert "calculator" in seeded
