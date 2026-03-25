"""Property tests for workflow concurrency configuration.

Feature: supply-chain-hardening, Property 12: All deployment workflows retain cancel-in-progress false
Validates: Requirements 15.2
"""

import glob
from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Indicators that a workflow contains CDK deploy operations
CDK_DEPLOY_INDICATORS = [
    "cdk deploy",
    "deploy.sh",
    "deploy-cdk.sh",
    "cdk destroy",
    "teardown.sh",
]


def _collect_workflow_files() -> list[Path]:
    """Collect all workflow YAML files."""
    return sorted(Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml")))


def _is_reusable_workflow(workflow: dict) -> bool:
    """Check if a workflow is a reusable workflow (triggered by workflow_call).

    Reusable workflows inherit concurrency from their caller, so they
    don't need their own concurrency block.
    """
    on_trigger = workflow.get("on", workflow.get(True, {}))
    if isinstance(on_trigger, dict):
        return "workflow_call" in on_trigger
    return False


def _workflow_has_cdk_deploy(workflow: dict) -> bool:
    """Check if a workflow contains any CDK deploy operations."""
    jobs = workflow.get("jobs", {})
    for job_name, job_config in jobs.items():
        if not isinstance(job_config, dict):
            continue

        # Check reusable workflow calls
        if "uses" in job_config:
            uses_val = str(job_config["uses"])
            if "deploy" in uses_val.lower():
                return True

        steps = job_config.get("steps", [])
        for step in steps:
            if not isinstance(step, dict):
                continue
            # Check run commands
            run_cmd = str(step.get("run", ""))
            for indicator in CDK_DEPLOY_INDICATORS:
                if indicator in run_cmd:
                    return True
            # Check step names
            step_name = str(step.get("name", "")).lower()
            if "deploy" in step_name and "cdk" in step_name:
                return True

    return False


def test_deployment_workflows_have_cancel_in_progress_false():
    """Property 12: All deployment workflows retain cancel-in-progress false.

    For any workflow that contains a CDK deploy job (including frontend,
    which deploys the CloudFront/S3 stack via CDK), the workflow's
    concurrency.cancel-in-progress must be false.

    Cancelling a CDK deploy mid-execution can leave CloudFormation in a
    ROLLBACK_IN_PROGRESS or UPDATE_ROLLBACK_FAILED state.

    **Validates: Requirements 15.2**
    """
    workflow_files = _collect_workflow_files()
    assert len(workflow_files) > 0, "No workflow YAML files found"

    deploy_workflows = []
    violations = []

    for wf_path in workflow_files:
        with open(wf_path) as f:
            workflow = yaml.safe_load(f)

        if not isinstance(workflow, dict):
            continue

        rel_path = str(wf_path.relative_to(REPO_ROOT))

        # Skip reusable workflows — they inherit concurrency from caller
        if _is_reusable_workflow(workflow):
            continue

        if not _workflow_has_cdk_deploy(workflow):
            continue

        deploy_workflows.append(rel_path)

        concurrency = workflow.get("concurrency", {})
        if not isinstance(concurrency, dict):
            # concurrency might be a string (group name only) — no cancel-in-progress
            # This is acceptable as the default is false
            continue

        cancel_in_progress = concurrency.get("cancel-in-progress")

        if cancel_in_progress is not False:
            violations.append(
                f"  {rel_path}: concurrency.cancel-in-progress = {cancel_in_progress} "
                f"(expected: false)"
            )

    assert len(deploy_workflows) > 0, (
        "No deployment workflows found. Expected at least one workflow "
        "with CDK deploy operations."
    )

    assert not violations, (
        f"Found {len(violations)} deployment workflow(s) with incorrect "
        f"cancel-in-progress setting:\n" + "\n".join(violations)
    )
