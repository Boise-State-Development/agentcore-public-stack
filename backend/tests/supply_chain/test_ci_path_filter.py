"""The PR gate runs only the suites a change can reach.

ci.yml's ``changes`` job feeds the pull request's file list through
``scripts/ci/classify-changes.sh`` and wires each output to a ``tests.yml``
input. These tests pin three things:

1. The classifier's rules — which paths reach which suite — and above all its
   fail-open cases: an unknown directory, an empty list, a workflow or script
   change all run everything. Only an explicit allowlist runs nothing.
2. That every ``run_*`` input ``tests.yml`` offers is wired on the PR gate. A
   new suite whose input ci.yml never passes is a suite that runs nowhere on a
   pull request, and nothing else would notice.
3. That the backend job fans out over logical CPUs. ``-n auto`` counts
   physical cores when psutil is installed, which on the 4-vCPU runner is 2.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "classify-changes.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

SUITES = ("backend", "backend_contracts", "frontend", "infra", "load", "scripts")


def classify(paths: list[str]) -> dict[str, bool]:
    stdin = "".join(f"{p}\n" for p in paths)
    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, bool] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        assert value in ("true", "false"), line
        result[key] = value == "true"
    assert set(result) == set(SUITES), completed.stdout
    return result


def on(*names: str) -> dict[str, bool]:
    unknown = set(names) - set(SUITES)
    assert not unknown, unknown
    return {suite: suite in names for suite in SUITES}


ALL = on(*SUITES)
NONE = on()


def _load_workflow(name: str) -> dict:
    with open(WORKFLOWS / name) as f:
        return yaml.safe_load(f)


def _triggers(workflow: dict) -> dict:
    # PyYAML reads a bare `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True))


class TestRules:
    @pytest.mark.parametrize(
        "paths, expected",
        [
            # --- nothing reads these -----------------------------------
            (["docs-site/src/content/docs/configuration/feature-flags.md"], NONE),
            (["docs/specs/turn-path-ttft.md", "docs/kaizen/review-queue.md"], NONE),
            (["CHANGELOG.md", "README.md", "CONTRIBUTING.md", "CLAUDE.MD"], NONE),
            (["LICENSE", ".gitignore"], NONE),
            (["tui/src/app.py", "tui/pyproject.toml"], NONE),
            # --- one package ---------------------------------------------
            (["backend/src/apis/shared/feature_flags.py"], on("backend", "load")),
            (["backend/tests/shared/test_anything.py"], on("backend", "load")),
            (["backend/pyproject.toml", "backend/uv.lock"], on("backend", "load")),
            (["backend/README.md"], on("backend", "load")),
            (["frontend/ai.client/src/app/app.ts"], on("frontend")),
            (["frontend/ai.client/src/branding/README.md"], on("frontend")),
            (["infrastructure/lib/config.ts"], on("infra", "backend_contracts")),
            (["infrastructure/test/platform-stack.test.ts"], on("infra", "backend_contracts")),
            (["tests/supply_chain/test_backup_coverage.py"], on("backend_contracts")),
            (["tests/load/locustfile.py"], on("load")),
            # --- cross-package reads pinned by name ----------------------
            # pending-backfills.test.ts asserts the backfill scripts exist.
            (["backend/scripts/backfill_something.py"], on("backend", "load", "infra")),
            # pending-backfills.test.ts reads RELEASE_NOTES.md.
            (["RELEASE_NOTES.md"], on("infra", "backend_contracts")),
            # gsi-update-limit.test.ts asserts against the release skill.
            ([".claude/skills/cutting-a-release/SKILL.md"], on("infra")),
            # --- unions ----------------------------------------------------
            (
                ["frontend/ai.client/src/app/app.ts", "backend/src/x.py", "docs/x.md"],
                on("frontend", "backend", "load"),
            ),
            (
                ["infrastructure/lib/x.ts", "backend/src/x.py"],
                on("infra", "backend_contracts", "backend", "load"),
            ),
        ],
    )
    def test_paths_reach_exactly_the_suites_that_read_them(self, paths, expected):
        assert classify(paths) == expected

    @pytest.mark.parametrize(
        "paths",
        [
            # No PR, or the file-list call failed: nothing to reason from.
            [],
            [""],
            # Workflows and composite actions are read by the supply-chain
            # suites and shape every job.
            [".github/workflows/tests.yml"],
            [".github/workflows/ci.yml"],
            [".github/actions/something/action.yml"],
            [".github/dependabot.yml"],
            [".github/ARTIFACT_RETENTION.md"],
            # scripts/ feeds four different suites.
            ["scripts/frontend/test.sh"],
            ["scripts/release/check-pending-backfills.mjs"],
            ["scripts/restore-data/restore.py"],
            ["scripts/ci/classify-changes.sh"],
            # A directory this script has never heard of.
            ["brand-new-package/src/main.py"],
            # A root file this script has never heard of.
            ["Makefile"],
            ["VERSION"],
            # One unknown path in a docs PR still runs everything.
            ["docs/x.md", "brand-new-package/main.py"],
        ],
    )
    def test_anything_unrecognised_runs_everything(self, paths):
        assert classify(paths) == ALL

    def test_a_rename_is_classified_under_both_names(self):
        # ci.yml feeds `.filename` and `.previous_filename` as separate lines.
        assert classify(["frontend/new.ts", "backend/old.py"]) == on(
            "frontend", "backend", "load"
        )


class TestWiring:
    def test_ci_wires_every_suite_tests_yml_offers(self):
        ci = _load_workflow("ci.yml")
        tests = _load_workflow("tests.yml")

        offered = {
            name
            for name in _triggers(tests)["workflow_call"]["inputs"]
            if name.startswith("run_")
        }
        wired = ci["jobs"]["tests"]["with"]
        assert set(wired) == offered, (
            "ci.yml must pass every run_* input tests.yml declares; "
            f"missing={offered - set(wired)} extra={set(wired) - offered}"
        )
        for name, value in wired.items():
            assert isinstance(value, str) and "needs.changes.outputs." in value, (
                f"{name} must be decided by the changes job, not hard-coded: {value!r}"
            )

    def test_changes_job_exposes_every_classifier_output(self):
        ci = _load_workflow("ci.yml")
        changes = ci["jobs"]["changes"]
        assert set(changes["outputs"]) == set(SUITES)
        assert ci["jobs"]["tests"]["needs"] == "changes"
        run_blocks = "\n".join(step.get("run", "") for step in changes["steps"])
        assert "scripts/ci/classify-changes.sh" in run_blocks

    def test_changes_job_only_reads(self):
        ci = _load_workflow("ci.yml")
        perms = ci["jobs"]["changes"]["permissions"]
        assert perms == {"contents": "read", "pull-requests": "read"}

    def test_contracts_job_yields_to_the_full_backend_run(self):
        tests = _load_workflow("tests.yml")
        job = tests["jobs"]["test-backend-contracts"]
        assert "inputs.run_backend_contracts" in job["if"]
        assert "!inputs.run_backend" in job["if"]

    def test_backend_pytest_fans_out_over_logical_cpus(self):
        tests = _load_workflow("tests.yml")
        steps = tests["jobs"]["test-backend"]["steps"]
        pytest_step = next(s for s in steps if s.get("name") == "Run pytest")
        assert "-n logical" in pytest_step["run"]
        assert "-n auto" not in pytest_step["run"]
