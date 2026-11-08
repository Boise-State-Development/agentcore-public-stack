"""``evals/tool_spec_tokens.json`` must cover the seeded catalog and match its schemas.

The release ledger's tools table shows each tool's *spec* cost (what its schema
adds to the cached ``toolConfig`` every turn it is enabled) beside its per-call
payload (release-ledger spec D4, §10 Q3). The figures are a committed CountTokens
measurement, so nothing at runtime keeps them honest; this test does.

It fails when:

* a ``toolId`` in ``DEFAULT_TOOLS`` (the seeder, the only writer of ``TOOL#``
  rows) has no row — a new tool would otherwise show no spec cost, silently;
* a row's ``specHash`` no longer matches the family the turn path builds — the
  schema changed (a reworded description counts: it is tokens) and the number
  is stale.

The fix for either is to re-run the generator against an AWS account, which this
test cannot do (it never calls AWS)::

    eval "$(aws configure export-credentials --profile dev-ai --format env)"; unset AWS_PROFILE
    .venv/bin/python scripts/measure_tool_spec_tokens.py --region us-west-2
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_SCRIPT = _BACKEND / "scripts" / "measure_tool_spec_tokens.py"
_spec = importlib.util.spec_from_file_location("measure_tool_spec_tokens", _SCRIPT)
measure = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = measure
_spec.loader.exec_module(measure)

_FIX = "re-run: .venv/bin/python scripts/measure_tool_spec_tokens.py --region us-west-2 (needs AWS)"


@pytest.fixture(scope="module")
def committed():
    return json.loads(measure.OUTPUT.read_text())


@pytest.fixture(scope="module")
def specs():
    # The generator forces the family flags on in os.environ; restore them so
    # no other test in the run sees a flipped flag.
    import os

    saved = {key: os.environ.get(key) for key in measure.MEASURE_ENV}
    try:
        yield measure.build_catalog_tool_specs()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_every_seeded_tool_has_a_row(committed):
    missing = sorted(set(measure.seeded_tool_ids()) - set(committed["tools"]))
    assert not missing, f"seeded tools with no spec-token row: {missing}; {_FIX}"


def test_no_row_is_stale(committed, specs):
    stale = measure.stale_rows(committed, specs)
    assert not stale, f"stale spec-token rows: {stale}; {_FIX}"


def test_rows_are_numbers_and_names_only(committed):
    """Content-free: ids, counts, lengths, hashes. No schema text is committed."""
    for tool_id, row in committed["tools"].items():
        assert set(row) <= {"tokens", "tools", "chars", "specHash", "note"}, tool_id
        assert row["tokens"] is None or (isinstance(row["tokens"], int) and row["tokens"] > 0), tool_id
        assert all(isinstance(n, int) and n > 0 for n in row["tools"].values()), tool_id
        assert len(row["specHash"]) == 16


def test_each_row_names_the_tools_its_family_builds(committed, specs):
    """A census entry is keyed by tool *name*; the row's member map is how the
    tools table joins one to its spec cost, so the names must be current."""
    for tool_id, family in specs.items():
        assert sorted(committed["tools"][tool_id]["tools"]) == [s["name"] for s in family], tool_id


def test_the_hash_moves_when_a_schema_does():
    family = [{"name": "t", "description": "a", "inputSchema": {"json": {"type": "object"}}}]
    changed = [{**family[0], "description": "b"}]
    assert measure.spec_hash(family) == measure.spec_hash(json.loads(json.dumps(family)))
    assert measure.spec_hash(family) != measure.spec_hash(changed)
