"""The bootstrap seeder's tool rows must carry the same index keys the model writes.

`seed_bootstrap_data.py` hand-builds its tool item instead of going through
`ToolDefinition.to_dynamo_item`, so every index key exists in TWO places and a
new one can be added to the model while the seeder is forgotten.

That omission is invisible in the worst way. A freshly bootstrapped deployment —
a fork, a new environment, a rebuilt dev — would seed tools with no index keys,
and once the catalog read moves to `EntityTypeIndex` it would list ZERO tools. No
error: a sparse index answers "nothing matched", not "something is wrong". And a
backfill would not be the fix, because nothing about that install is legacy.

This test is the thing that fails instead.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from apis.shared.tools.models import ENTITY_TYPE_TOOL, ToolDefinition  # noqa: E402

SEED_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "seed_bootstrap_data.py"
)


def _model_index_keys() -> set[str]:
    """Every GSI key attribute `to_dynamo_item` writes on a tool row."""
    item = ToolDefinition(
        tool_id="probe",
        display_name="Probe",
        description="d",
        category="utility",
        protocol="local",
    ).to_dynamo_item()
    return {k for k in item if re.fullmatch(r"GSI\d+(PK|SK)", k)}


def _seeded_tool_block() -> str:
    """The seeder's hand-built tool item literal."""
    source = open(SEED_SCRIPT, encoding="utf-8").read()
    start = source.index('"GSI1PK": f"CATEGORY#')
    return source[start : start + 1200]


class TestSeederMirrorsTheModel:
    def test_seeder_writes_every_index_key_the_model_does(self):
        block = _seeded_tool_block()
        missing = sorted(k for k in _model_index_keys() if f'"{k}"' not in block)

        assert not missing, (
            f"seed_bootstrap_data.py does not write {missing} on its tool rows. "
            "It hand-builds the item rather than calling "
            "ToolDefinition.to_dynamo_item, so a new index key must be added in "
            "both places — otherwise a freshly bootstrapped deployment is absent "
            "from that index, silently."
        )

    def test_seeder_uses_the_shared_entity_type_constant(self):
        """A literal that drifts from ENTITY_TYPE_TOOL puts seeded tools in a
        partition the reader never queries — the same silent-empty failure."""
        assert f'"{ENTITY_TYPE_TOOL}"' in _seeded_tool_block()

    def test_model_is_the_source_of_truth_for_this_test(self):
        """Guards the test itself: if the probe stops producing index keys the
        assertions above would pass vacuously."""
        keys = _model_index_keys()
        assert {"GSI1PK", "GSI1SK", "GSI5PK", "GSI5SK"} <= keys
