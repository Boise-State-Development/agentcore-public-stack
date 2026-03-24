"""Property tests for artifact retention consistency.

Feature: supply-chain-hardening, Property 11: Consistent artifact retention per artifact type
Validates: Requirements 14.2
"""

import glob
from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Artifact categories and their expected retention days.
# Each artifact name pattern maps to a category.
ARTIFACT_CATEGORIES = {
    "docker-image": {
        "patterns": ["docker-image"],
        "expected_days": 1,
    },
    "cdk-synth": {
        "patterns": ["cdk-synth"],
        "expected_days": 7,
    },
    "build-artifacts": {
        "patterns": ["infrastructure-build", "cdk-templates"],
        "expected_days": 1,
    },
    "per-pr-test-results": {
        "patterns": ["test-results"],
        "expected_days": 7,
    },
    "nightly-coverage": {
        "patterns": ["coverage"],
        "expected_days": 30,
    },
    "deployment-outputs": {
        "patterns": ["outputs"],
        "expected_days": 30,
    },
    "scan-reports": {
        "patterns": ["scan-reports", "trivy"],
        "expected_days": 30,
    },
}


def _collect_workflow_files() -> list[Path]:
    """Collect all workflow YAML files."""
    return sorted(Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml")))


def _find_upload_artifact_steps(workflow: dict) -> list[dict]:
    """Find all upload-artifact steps across all jobs in a workflow."""
    steps_found = []
    jobs = workflow.get("jobs", {})
    for job_name, job_config in jobs.items():
        if not isinstance(job_config, dict):
            continue
        steps = job_config.get("steps", [])
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses", "")
            if isinstance(uses, str) and "upload-artifact" in uses:
                steps_found.append(step)
    return steps_found


def test_artifact_retention_is_consistent_within_categories():
    """Property 11: Consistent artifact retention per artifact type.

    For any two upload-artifact steps across all workflow files that upload
    the same category of artifact, the retention-days value must be identical.

    Categories:
    - Docker image tarballs: 1 day
    - CDK synth templates: 7 days
    - Build artifacts: 1 day
    - Deployment outputs: 30 days
    - Scan reports: 30 days

    **Validates: Requirements 14.2**
    """
    workflow_files = _collect_workflow_files()
    assert len(workflow_files) > 0, "No workflow YAML files found"

    # Collect all upload-artifact steps with their retention-days
    all_artifacts: list[tuple[str, str, int]] = []  # (file, artifact_name, retention_days)

    for wf_path in workflow_files:
        with open(wf_path) as f:
            workflow = yaml.safe_load(f)

        if not isinstance(workflow, dict):
            continue

        rel_path = str(wf_path.relative_to(REPO_ROOT))
        upload_steps = _find_upload_artifact_steps(workflow)

        for step in upload_steps:
            with_block = step.get("with", {})
            name = with_block.get("name", "unnamed")
            retention = with_block.get("retention-days")
            if retention is not None:
                all_artifacts.append((rel_path, str(name), int(retention)))

    assert len(all_artifacts) > 0, (
        "No upload-artifact steps with retention-days found in any workflow"
    )

    # Group by category and check consistency
    category_retentions: dict[str, list[tuple[str, str, int]]] = {}

    for file_path, artifact_name, retention in all_artifacts:
        for cat_name, cat_info in ARTIFACT_CATEGORIES.items():
            if any(p in artifact_name for p in cat_info["patterns"]):
                category_retentions.setdefault(cat_name, []).append(
                    (file_path, artifact_name, retention)
                )
                break

    violations = []
    for cat_name, entries in category_retentions.items():
        retention_values = {r for _, _, r in entries}
        if len(retention_values) > 1:
            details = "\n".join(
                f"    {f}: {n} = {r} days" for f, n, r in entries
            )
            violations.append(
                f"  Category '{cat_name}' has inconsistent retention:\n{details}"
            )

    assert not violations, (
        f"Found inconsistent artifact retention within categories:\n"
        + "\n".join(violations)
    )


def test_all_upload_artifacts_have_retention_days():
    """Verify all upload-artifact steps specify retention-days explicitly.

    **Validates: Requirements 14.2**
    """
    workflow_files = _collect_workflow_files()

    missing = []

    for wf_path in workflow_files:
        with open(wf_path) as f:
            workflow = yaml.safe_load(f)

        if not isinstance(workflow, dict):
            continue

        rel_path = str(wf_path.relative_to(REPO_ROOT))
        upload_steps = _find_upload_artifact_steps(workflow)

        for step in upload_steps:
            with_block = step.get("with", {})
            name = with_block.get("name", "unnamed")
            if "retention-days" not in with_block:
                missing.append(f"  {rel_path}: artifact '{name}' missing retention-days")

    assert not missing, (
        f"Found {len(missing)} upload-artifact step(s) without retention-days:\n"
        + "\n".join(missing)
    )
