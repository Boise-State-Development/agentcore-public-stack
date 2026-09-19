"""Every input the Platform Stack deploy consumes must also trigger it.

A path the workflow reads but does not watch is the worst kind of deploy bug:
the push is green, no workflow fails, and the old artifact silently stays live.
That happened with the browser sign-in viewer — `infrastructure/assets/**` is
staged into the mcp-sandbox bucket at synth, but nothing in `paths:` matched
it, so a fix to it deployed nowhere while CI reported success.

These tests derive the expectation from what the workflow and the CDK tree
actually do, rather than restating the `paths:` list back at itself.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PLATFORM_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform.yml"


def _trigger_paths() -> list[str]:
    # `on` is parsed by PyYAML as the boolean True (YAML 1.1), so accept either.
    config = yaml.safe_load(PLATFORM_WORKFLOW.read_text())
    triggers = config.get("on", config.get(True))
    return list(triggers["push"]["paths"])


def _covers(patterns: list[str], repo_relative: str) -> bool:
    """True when some glob in `patterns` would match `repo_relative`.

    `repo_relative` may name a file or a directory. A `dir/**` pattern covers
    the directory itself as well as anything beneath it, which `Path.match`
    alone does not give us.
    """
    for pattern in patterns:
        if Path(repo_relative).match(pattern):
            return True
        prefix = pattern.removesuffix("**").rstrip("/")
        if prefix and (repo_relative == prefix or repo_relative.startswith(prefix + "/")):
            return True
    return False


def test_scripts_the_workflow_runs_also_trigger_it() -> None:
    """A script invoked by the deploy job must be watched by it.

    `fetch-dcv-sdk.sh` pins a version, a SHA256 and a GPG fingerprint. Bumping
    any of them is precisely a change that must redeploy.
    """
    body = PLATFORM_WORKFLOW.read_text()
    patterns = _trigger_paths()

    invoked = {
        line.split("bash ", 1)[1].strip()
        for line in body.splitlines()
        if "bash scripts/" in line
    }
    assert invoked, "expected the deploy job to invoke at least one script"

    unwatched = sorted(s for s in invoked if not _covers(patterns, s))
    assert not unwatched, (
        "platform.yml runs these scripts but would not redeploy when they "
        f"change: {unwatched}"
    )


def test_cdk_staged_asset_directories_trigger_a_deploy() -> None:
    """Directories CDK stages at synth must be watched.

    Found by walking the constructs for staged directories rather than naming
    them here, so a new staged directory is covered the day it is added.
    """
    patterns = _trigger_paths()
    constructs = REPO_ROOT / "infrastructure" / "lib" / "constructs"

    staged: set[str] = set()
    for source in constructs.rglob("*.ts"):
        text = source.read_text()
        for marker in ("'assets'", '"assets"'):
            if marker in text:
                staged.add("infrastructure/assets")

    assert staged, "expected to find at least one staged asset directory"

    unwatched = sorted(d for d in staged if not _covers(patterns, d))
    assert not unwatched, (
        "CDK stages these directories at synth, but a change to them would "
        f"not trigger a Platform Stack deploy: {unwatched}"
    )


def test_the_viewer_asset_specifically_triggers_a_deploy() -> None:
    """Regression guard for the exact file that was stranded."""
    viewer = "infrastructure/assets/mcp-sandbox/live-view.js"
    assert (REPO_ROOT / viewer).exists(), f"{viewer} moved; update this guard"
    assert _covers(_trigger_paths(), viewer), (
        f"{viewer} is deployed by platform.yml but would not trigger it"
    )
