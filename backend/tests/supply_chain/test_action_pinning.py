"""Property tests for GitHub Actions SHA pinning.

Feature: supply-chain-hardening, Property 1: Third-party actions are SHA-pinned
Feature: supply-chain-hardening, Property 10: Consistent checkout action SHA across all workflows
Validates: Requirements 1.1, 13.1
"""

import glob
import re
from pathlib import Path

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
COMPOSITE_ACTION = (
    REPO_ROOT / ".github" / "actions" / "configure-aws-credentials" / "action.yml"
)

# Pattern for a properly SHA-pinned action with version comment:
#   owner/action@<40-hex-char> # vX.Y.Z
# The version comment part uses # which is a YAML comment, so we must
# parse raw lines rather than relying on the YAML parser.
SHA_PIN_PATTERN = re.compile(
    r"^\s*uses:\s+"
    r"[\w.-]+/[\w.-]+(/[\w.-]+)*"  # owner/action (with optional sub-action)
    r"@[0-9a-f]{40}"               # @<40-char-hex-sha>
    r"\s+#\s+v[\d]+[\d.]*"         # # vX.Y.Z version comment
)

# Pattern to detect any uses: line (third-party, not local)
USES_LINE_PATTERN = re.compile(
    r"^\s*uses:\s+(?!\./)(\S+)"
)

LOCAL_ACTION_PREFIX = "./"

# Pattern to extract SHA from an actions/checkout@<sha> reference
CHECKOUT_SHA_PATTERN = re.compile(
    r"actions/checkout@([0-9a-f]{40})"
)


def _collect_yaml_files() -> list[Path]:
    """Collect all workflow YAML files and the composite action."""
    files = sorted(Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml")))
    if COMPOSITE_ACTION.exists():
        files.append(COMPOSITE_ACTION)
    return files


def _collect_all_uses_lines() -> list[tuple[str, int, str]]:
    """Parse raw lines from all YAML files, return (file, line_no, line) for uses: lines."""
    results = []
    for yaml_path in _collect_yaml_files():
        with open(yaml_path) as f:
            for line_no, line in enumerate(f, start=1):
                stripped = line.strip()
                if stripped.startswith("uses:") or stripped.startswith("- uses:"):
                    # Normalize "- uses:" to "uses:" for pattern matching
                    short_path = str(yaml_path.relative_to(REPO_ROOT))
                    results.append((short_path, line_no, line.rstrip()))
    return results


def test_all_third_party_actions_are_sha_pinned():
    """Property 1: Third-party actions are SHA-pinned with version comments.

    For any uses: reference in any workflow YAML or composite action YAML,
    if the reference is to a third-party action (not starting with './'),
    then the raw line must match: owner/action@<40-char-hex> # vX.Y.Z

    **Validates: Requirements 1.1**
    """
    all_lines = _collect_all_uses_lines()
    assert len(all_lines) > 0, "No uses: references found across all YAML files"

    violations = []
    for file_path, line_no, raw_line in all_lines:
        # Skip local composite action references (starting with ./)
        if LOCAL_ACTION_PREFIX in raw_line.split("uses:")[-1].strip()[:2]:
            continue

        # Check if the raw line matches the SHA pin pattern with version comment
        # We need to handle both "uses:" and "- uses:" prefixes
        check_line = raw_line
        if "- uses:" in check_line:
            # Normalize "- uses:" to "uses:" for pattern matching
            check_line = check_line.replace("- uses:", "uses:", 1)

        if not SHA_PIN_PATTERN.search(check_line):
            violations.append(
                f"  {file_path}:{line_no}: {raw_line.strip()}"
            )

    assert not violations, (
        f"Found {len(violations)} third-party action(s) not SHA-pinned "
        f"with version comment (expected: owner/action@<sha> # vX.Y.Z):\n"
        + "\n".join(violations)
    )


def test_local_composite_actions_are_exempt_from_sha_pinning():
    """Verify local composite action references (starting with ./) are exempt.

    **Validates: Requirements 1.4**
    """
    all_lines = _collect_all_uses_lines()

    local_refs = []
    for file_path, line_no, raw_line in all_lines:
        uses_value = raw_line.split("uses:")[-1].strip()
        if uses_value.startswith(LOCAL_ACTION_PREFIX):
            local_refs.append((file_path, line_no, raw_line))

    # We expect at least some local action references exist
    assert len(local_refs) > 0, (
        "Expected at least one local composite action reference (starting with './') "
        "but found none"
    )

    # Local refs should NOT need SHA pinning — just verify they exist
    for file_path, line_no, raw_line in local_refs:
        uses_value = raw_line.split("uses:")[-1].strip()
        assert uses_value.startswith(LOCAL_ACTION_PREFIX), (
            f"Local action ref doesn't start with './': {uses_value} "
            f"at {file_path}:{line_no}"
        )


def test_consistent_checkout_sha_across_all_workflows():
    """Property 10: Consistent checkout action SHA across all workflows.

    For any two workflow YAML files that reference actions/checkout,
    the SHA digest used must be identical.

    **Validates: Requirements 13.1**
    """
    # Only check workflow files (not composite action) for checkout consistency
    workflow_files = sorted(
        Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml"))
    )
    assert len(workflow_files) > 0, "No workflow YAML files found"

    # Collect all checkout SHAs per file
    checkout_shas: dict[str, set[str]] = {}
    for yaml_path in workflow_files:
        short_path = str(yaml_path.relative_to(REPO_ROOT))
        with open(yaml_path) as f:
            for line in f:
                match = CHECKOUT_SHA_PATTERN.search(line)
                if match:
                    sha = match.group(1)
                    checkout_shas.setdefault(short_path, set()).add(sha)

    assert len(checkout_shas) > 0, (
        "No actions/checkout references found in any workflow file"
    )

    # Gather all unique SHAs across all files
    all_shas: set[str] = set()
    for shas in checkout_shas.values():
        all_shas.update(shas)

    assert len(all_shas) == 1, (
        f"Expected all workflows to use the same actions/checkout SHA, "
        f"but found {len(all_shas)} distinct SHA(s):\n"
        + "\n".join(
            f"  {path}: {', '.join(sorted(shas))}"
            for path, shas in sorted(checkout_shas.items())
        )
    )
