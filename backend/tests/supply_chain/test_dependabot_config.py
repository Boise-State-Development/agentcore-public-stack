"""Property tests for Dependabot configuration.

Feature: supply-chain-hardening, Property 8: Dependabot entries target develop with grouped updates
Validates: Requirements 9.2, 9.3
"""

from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"


def _load_dependabot_config() -> dict:
    """Load and parse the Dependabot configuration."""
    assert DEPENDABOT_PATH.exists(), (
        f"Dependabot config not found at {DEPENDABOT_PATH.relative_to(REPO_ROOT)}"
    )
    with open(DEPENDABOT_PATH) as f:
        return yaml.safe_load(f)


def test_all_ecosystems_target_develop():
    """Property 8a: All Dependabot ecosystem entries target the develop branch.

    **Validates: Requirement 9.2**
    """
    config = _load_dependabot_config()
    updates = config.get("updates", [])
    assert len(updates) > 0, "No ecosystem entries found in dependabot.yml"

    violations = []
    for entry in updates:
        ecosystem = entry.get("package-ecosystem", "unknown")
        directory = entry.get("directory", "/")
        target = entry.get("target-branch")

        if target != "develop":
            violations.append(
                f"  {ecosystem} ({directory}): target-branch = '{target}' "
                f"(expected: 'develop')"
            )

    assert not violations, (
        f"Found {len(violations)} ecosystem(s) not targeting develop:\n"
        + "\n".join(violations)
    )


def test_all_ecosystems_have_grouped_updates():
    """Property 8b: All Dependabot entries have groups covering minor and patch.

    Each ecosystem entry must contain a `groups` section with at least one
    group that includes both "minor" and "patch" in its `update-types`.

    **Validates: Requirement 9.3**
    """
    config = _load_dependabot_config()
    updates = config.get("updates", [])
    assert len(updates) > 0, "No ecosystem entries found in dependabot.yml"

    violations = []
    for entry in updates:
        ecosystem = entry.get("package-ecosystem", "unknown")
        directory = entry.get("directory", "/")
        groups = entry.get("groups", {})

        if not groups:
            violations.append(
                f"  {ecosystem} ({directory}): no 'groups' section found"
            )
            continue

        # Collect all update-types across all groups for this ecosystem
        all_update_types: set[str] = set()
        for group_name, group_config in groups.items():
            update_types = group_config.get("update-types", [])
            all_update_types.update(update_types)

        missing = {"minor", "patch"} - all_update_types
        if missing:
            violations.append(
                f"  {ecosystem} ({directory}): groups missing update-types: "
                f"{', '.join(sorted(missing))}"
            )

    assert not violations, (
        f"Found {len(violations)} ecosystem(s) without proper grouped updates:\n"
        + "\n".join(violations)
    )
