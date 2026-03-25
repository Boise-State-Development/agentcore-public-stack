"""Property tests for AWS credential scoping in GitHub Actions workflows.

Feature: supply-chain-hardening, Property 14: AWS credentials scoped to AWS-using jobs only
Validates: Requirements 17.1, 17.2
"""

import glob
from pathlib import Path

import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# AWS credential variable names that should never appear at workflow level
AWS_CREDENTIAL_VARS = {
    "AWS_ROLE_ARN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}


def _collect_workflow_files() -> list[Path]:
    """Collect all workflow YAML files."""
    return sorted(Path(f) for f in glob.glob(str(WORKFLOWS_DIR / "*.yml")))


def test_no_aws_credentials_in_workflow_level_env():
    """Property 14: No AWS credentials appear in workflow-level env blocks.

    For any workflow YAML file, the top-level `env:` block must NOT contain
    AWS credential variables (AWS_ROLE_ARN, AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN).

    AWS credentials should only appear in job-level `env:` blocks on jobs
    that actually interact with AWS.

    **Validates: Requirements 17.1, 17.2**
    """
    workflow_files = _collect_workflow_files()
    assert len(workflow_files) > 0, "No workflow YAML files found"

    violations = []

    for wf_path in workflow_files:
        with open(wf_path) as f:
            workflow = yaml.safe_load(f)

        if not isinstance(workflow, dict):
            continue

        rel_path = str(wf_path.relative_to(REPO_ROOT))

        # Check workflow-level env block
        workflow_env = workflow.get("env", {})
        if not isinstance(workflow_env, dict):
            continue

        for var_name in workflow_env:
            if var_name in AWS_CREDENTIAL_VARS:
                violations.append(
                    f"  {rel_path}: workflow-level env contains "
                    f"AWS credential '{var_name}' — move to job-level env"
                )

    assert not violations, (
        f"Found {len(violations)} AWS credential(s) in workflow-level env blocks "
        f"(should be job-level only):\n" + "\n".join(violations)
    )


def test_aws_credentials_only_on_aws_jobs():
    """Verify AWS credentials at job-level only appear on jobs with AWS steps.

    For any job that has AWS credentials in its env block, the job must
    contain at least one step that interacts with AWS (configure-aws-credentials,
    ECR operations, CDK deploy, S3 operations, etc.).

    **Validates: Requirements 17.1, 17.2**
    """
    workflow_files = _collect_workflow_files()
    assert len(workflow_files) > 0, "No workflow YAML files found"

    # Patterns that indicate a job interacts with AWS
    aws_step_indicators = [
        "configure-aws-credentials",
        "aws ",  # AWS CLI commands
        "cdk deploy",
        "cdk synth",
        "cdk diff",
        "cdk destroy",
        "ecr",
        "aws-actions",
        "load-env.sh",  # loads AWS config
        "deploy.sh",
        "synth.sh",
        "push-to-ecr.sh",
        "smoke-test.sh",
        "deploy-assets.sh",
        "deploy-cdk.sh",
        "seed.sh",
        "teardown.sh",
    ]

    jobs_with_creds = 0
    violations = []

    for wf_path in workflow_files:
        with open(wf_path) as f:
            workflow = yaml.safe_load(f)

        if not isinstance(workflow, dict):
            continue

        rel_path = str(wf_path.relative_to(REPO_ROOT))
        jobs = workflow.get("jobs", {})

        for job_name, job_config in jobs.items():
            if not isinstance(job_config, dict):
                continue

            job_env = job_config.get("env", {})
            if not isinstance(job_env, dict):
                continue

            # Check if this job has AWS credentials
            has_aws_creds = any(
                var in AWS_CREDENTIAL_VARS for var in job_env
            )

            if not has_aws_creds:
                continue

            jobs_with_creds += 1

            # Check if the job has AWS-interacting steps
            steps = job_config.get("steps", [])
            job_text = str(steps).lower()

            has_aws_step = any(
                indicator.lower() in job_text
                for indicator in aws_step_indicators
            )

            # Also check if the job uses a reusable workflow (uses: key at job level)
            if "uses" in job_config:
                has_aws_step = True

            if not has_aws_step:
                violations.append(
                    f"  {rel_path} → job '{job_name}': has AWS credentials "
                    f"but no AWS-interacting steps detected"
                )

    assert jobs_with_creds > 0, (
        "No jobs found with AWS credentials in env blocks. "
        "Expected at least some jobs to have AWS credentials."
    )

    assert not violations, (
        f"Found {len(violations)} job(s) with AWS credentials but no AWS steps:\n"
        + "\n".join(violations)
    )
