"""The seed's flat SKILL.md parser agrees with the shared, hardened one.

``seed_bootstrap_data.load_seed_skill_bundle`` keeps its own parser because the
bootstrap environment has no PyYAML. This pins it to
``apis.shared.skills.skill_md.parse_skill_md`` for every seeded bundle, so a
bundle the two would read differently fails here instead of seeding a row the
rest of the platform would read another way.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_bootstrap_data as seed  # noqa: E402
from apis.shared.skills.skill_md import parse_skill_md  # noqa: E402

BUNDLES = sorted(p.parent for p in seed.SEED_SKILLS_DIR.glob("*/SKILL.md"))


def test_there_are_seed_bundles_to_compare():
    assert seed.SEED_SKILLS_DIR / seed.SKILL_CREATOR_ID in BUNDLES


@pytest.mark.parametrize("directory", BUNDLES, ids=lambda p: p.name)
def test_seed_parser_matches_the_shared_parser(directory):
    flat = seed.load_seed_skill_bundle(directory)
    shared = parse_skill_md((directory / "SKILL.md").read_text(encoding="utf-8"))

    assert flat.frontmatter == shared.frontmatter
    assert flat.instructions == shared.body + "\n"
    assert shared.diagnostics == []
