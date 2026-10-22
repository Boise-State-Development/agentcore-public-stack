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


# ---------------------------------------------------------------------------
# Eviction guard.
#
# platform.yml and backend.yml share the "deploy-<ref>" concurrency group so a
# CFN deploy never mutates the same ECS service / AgentCore Runtime / Lambda as
# the API-driven code deploys. But a GitHub concurrency group has a queue depth
# of ONE — "any existing pending job or workflow in the same concurrency group
# will be canceled and the new queued job or workflow will take its place" — so
# a third run entering an occupied group silently kills whichever run was
# waiting, with zero jobs executed.
#
# Across workflows that is a dropped deploy: a Backend Deploy run does not run
# `cdk deploy`, so evicting a pending Platform Stack run skips the
# infrastructure change entirely while every check stays green. Observed live on
# 2026-09-21 (merge 2601a87d on develop) and, before it, ten more times across
# both workflows — including once on main.
#
# The `recover-evicted-peer` job in each workflow detects that and re-dispatches
# the peer. These tests keep the two halves of that arrangement — the shared
# group and the guard that makes it safe — from drifting apart.
# ---------------------------------------------------------------------------

DEPLOY_PAIR = {
    "platform.yml": "backend.yml",
    "backend.yml": "platform.yml",
}

GUARD_JOB = "recover-evicted-peer"
GUARD_SCRIPT = "scripts/ci/recover-evicted-deploy.sh"


def _load_workflow(name: str) -> dict:
    with open(WORKFLOWS_DIR / name) as f:
        return yaml.safe_load(f)


def test_platform_and_backend_share_one_deploy_concurrency_group():
    """The two deploy workflows must stay mutually exclusive.

    They mutate the same ECS service, AgentCore Runtime and Lambdas, so they
    must never run concurrently. If this assertion is ever relaxed, the
    serialization has to be replaced by something else *first* — and the
    eviction guard below stops being necessary in its current form.
    """
    groups = {
        name: _load_workflow(name).get("concurrency", {}).get("group")
        for name in DEPLOY_PAIR
    }

    assert all(groups.values()), f"a deploy workflow has no concurrency group: {groups}"
    assert len(set(groups.values())) == 1, (
        "platform.yml and backend.yml no longer share a concurrency group "
        f"({groups}). They mutate the same AWS resources — if you split them, "
        "serialize them another way and revisit the recover-evicted-peer job, "
        "whose whole purpose is to make the SHARED group safe."
    )


def test_each_deploy_workflow_guards_its_peer_against_eviction():
    """Each deploy workflow must be able to recover the peer run it evicts.

    A run that takes the pending slot is the only party that knows the peer was
    dropped, because the eviction is invisible everywhere else: the run ends
    `cancelled` having executed no jobs, which reads exactly like a human
    pressing Cancel.
    """
    assert (REPO_ROOT / GUARD_SCRIPT).is_file(), f"{GUARD_SCRIPT} is missing"

    for name, peer in DEPLOY_PAIR.items():
        wf = _load_workflow(name)
        jobs = wf.get("jobs", {})

        assert GUARD_JOB in jobs, (
            f"{name} has no '{GUARD_JOB}' job. Without it, a peer deploy this "
            f"workflow evicts from the shared concurrency group is dropped "
            f"silently — CI stays green and nothing is deployed."
        )
        job = jobs[GUARD_JOB]

        runs = " ".join(str(step.get("run", "")) for step in job.get("steps", []))
        assert GUARD_SCRIPT in runs, f"{name}:{GUARD_JOB} does not call {GUARD_SCRIPT}"
        assert peer in runs, (
            f"{name}:{GUARD_JOB} must recover '{peer}' — the workflow it shares "
            f"the deploy concurrency group with — but its run step does not "
            f"reference it."
        )

        # `gh workflow run` needs actions:write; without it the guard can only
        # report the loss rather than repair it.
        assert job.get("permissions", {}).get("actions") == "write", (
            f"{name}:{GUARD_JOB} needs 'actions: write' to re-dispatch {peer}."
        )

        # No `needs`: the eviction has already happened by the time this run
        # starts, so the recovery dispatch should not wait on a deploy.
        assert "needs" not in job, (
            f"{name}:{GUARD_JOB} must not declare 'needs' — the peer was "
            f"evicted the moment this run started, and the recovery dispatch "
            f"should go out immediately rather than behind the deploy."
        )
