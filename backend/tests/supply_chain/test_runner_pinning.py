"""Property test for GitHub Actions runner version pinning.

Feature: supply-chain-hardening, Property 7: No workflow job uses floating runner aliases
Validates: Requirements 8.1
"""

import glob
from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _collect_workflow_files() -> list[Path]:
    """Collect all workflow YAML files."""
    return sorted(Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml")))


def _extract_runs_on_values(workflow_path: Path) -> list[tuple[str, str, str]]:
    """Extract all runs-on values from a workflow file.

    Returns a list of (job_name, runs_on_value, relative_file_path) tuples.
    """
    with open(workflow_path) as f:
        data = yaml.safe_load(f)

    if not data or "jobs" not in data:
        return []

    results = []
    short_path = str(workflow_path.relative_to(REPO_ROOT))
    for job_name, job_config in data["jobs"].items():
        if not isinstance(job_config, dict):
            continue
        runs_on = job_config.get("runs-on")
        if runs_on is None:
            continue
        # runs-on can be a string or a list; normalise to list
        if isinstance(runs_on, str):
            values = [runs_on]
        elif isinstance(runs_on, list):
            values = [str(v) for v in runs_on]
        else:
            values = [str(runs_on)]
        for val in values:
            results.append((job_name, val, short_path))
    return results


def test_no_workflow_job_uses_floating_runner_alias():
    """Property 7: No workflow job uses floating runner aliases.

    For any runs-on value in any job in any workflow YAML file,
    the value must not contain the string '-latest'. It must specify
    an explicit OS version (e.g., ubuntu-24.04).

    **Validates: Requirements 8.1**
    """
    workflow_files = _collect_workflow_files()
    assert len(workflow_files) > 0, "No workflow YAML files found"

    all_entries: list[tuple[str, str, str]] = []
    for wf in workflow_files:
        all_entries.extend(_extract_runs_on_values(wf))

    assert len(all_entries) > 0, "No runs-on values found across all workflow files"

    violations = []
    for job_name, runs_on_value, file_path in all_entries:
        if "-latest" in runs_on_value:
            violations.append(
                f"  {file_path} → job '{job_name}': runs-on: {runs_on_value}"
            )

    assert not violations, (
        f"Found {len(violations)} job(s) using floating runner aliases "
        f"(contains '-latest'):\n" + "\n".join(violations)
    )
