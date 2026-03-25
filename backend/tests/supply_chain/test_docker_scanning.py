"""Property tests for nightly Docker image scanning track.

Feature: supply-chain-hardening, Property 6: Nightly workflow includes image scanning track
Validates: Requirements 7.1, 7.4
"""

from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

EXPECTED_DOCKERFILES = [
    "Dockerfile.app-api",
    "Dockerfile.inference-api",
    "Dockerfile.rag-ingestion",
]


def _load_nightly_workflow() -> dict:
    """Load and parse the nightly workflow YAML."""
    assert NIGHTLY_WORKFLOW.exists(), (
        f"Nightly workflow not found at {NIGHTLY_WORKFLOW.relative_to(REPO_ROOT)}"
    )
    with open(NIGHTLY_WORKFLOW) as f:
        return yaml.safe_load(f)


def test_resolve_tracks_outputs_run_scan_images():
    """Property 6a: resolve-tracks job outputs include run_scan_images.

    The resolve-tracks job must declare run_scan_images and scan_images_ref
    as outputs so downstream jobs can conditionally run image scanning.

    **Validates: Requirements 7.1**
    """
    workflow = _load_nightly_workflow()
    jobs = workflow.get("jobs", {})

    assert "resolve-tracks" in jobs, "resolve-tracks job not found in nightly.yml"

    resolve_job = jobs["resolve-tracks"]
    outputs = resolve_job.get("outputs", {})

    assert "run_scan_images" in outputs, (
        "resolve-tracks job missing 'run_scan_images' output"
    )
    assert "scan_images_ref" in outputs, (
        "resolve-tracks job missing 'scan_images_ref' output"
    )


def test_scan_images_job_exists():
    """Property 6b: A scan-images job exists in the nightly workflow.

    **Validates: Requirements 7.1**
    """
    workflow = _load_nightly_workflow()
    jobs = workflow.get("jobs", {})

    assert "scan-images" in jobs, (
        "scan-images job not found in nightly.yml. "
        "Expected a job that builds and scans Docker images with Trivy."
    )


def test_scan_images_references_all_dockerfiles():
    """Property 6c: scan-images job references all three Dockerfiles.

    The scan-images job must build and scan all three Docker images:
    app-api, inference-api, and rag-ingestion.

    **Validates: Requirements 7.1, 7.4**
    """
    content = NIGHTLY_WORKFLOW.read_text()

    # Find the scan-images job section by looking for references to Dockerfiles
    # We check the raw content since step commands may reference Dockerfiles
    missing = []
    for dockerfile in EXPECTED_DOCKERFILES:
        if dockerfile not in content:
            missing.append(dockerfile)

    assert not missing, (
        f"scan-images job does not reference the following Dockerfiles: "
        f"{', '.join(missing)}. All three images must be scanned."
    )


def test_scan_images_uses_advisory_exit_code():
    """Property 6d: Trivy scans use exit-code '0' (advisory mode).

    The scan must NOT fail the job on findings — it's informational only.

    **Validates: Requirements 7.4**
    """
    workflow = _load_nightly_workflow()
    jobs = workflow.get("jobs", {})
    scan_job = jobs.get("scan-images", {})
    steps = scan_job.get("steps", [])

    trivy_steps = [
        s for s in steps
        if isinstance(s.get("uses", ""), str) and "trivy-action" in s.get("uses", "")
    ]

    assert len(trivy_steps) > 0, (
        "No Trivy action steps found in scan-images job"
    )

    for step in trivy_steps:
        with_block = step.get("with", {})
        exit_code = str(with_block.get("exit-code", ""))
        assert exit_code == "0", (
            f"Trivy step '{step.get('name', 'unnamed')}' has exit-code='{exit_code}', "
            f"expected '0' (advisory mode)"
        )


def test_scan_images_uploads_artifacts():
    """Property 6e: scan-images job uploads scan reports as artifacts.

    **Validates: Requirements 7.1**
    """
    workflow = _load_nightly_workflow()
    jobs = workflow.get("jobs", {})
    scan_job = jobs.get("scan-images", {})
    steps = scan_job.get("steps", [])

    upload_steps = [
        s for s in steps
        if isinstance(s.get("uses", ""), str) and "upload-artifact" in s.get("uses", "")
    ]

    assert len(upload_steps) > 0, (
        "scan-images job does not upload artifacts. "
        "Expected an upload-artifact step for Trivy scan reports."
    )


def test_scan_images_in_summary_needs():
    """Property 6f: summary job includes scan-images in its needs list.

    **Validates: Requirements 7.1**
    """
    workflow = _load_nightly_workflow()
    jobs = workflow.get("jobs", {})

    assert "summary" in jobs, "summary job not found in nightly.yml"

    summary_job = jobs["summary"]
    needs = summary_job.get("needs", [])

    assert "scan-images" in needs, (
        f"summary job needs list does not include 'scan-images'. "
        f"Current needs: {needs}"
    )
