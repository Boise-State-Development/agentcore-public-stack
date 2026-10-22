"""Guard the nightly workflow's branch allowlist.

``nightly.yml`` runs on ``schedule``/``workflow_dispatch``, so its jobs execute
in the context of the default branch and hold a token that can **write** the
default-branch GitHub Actions cache scope. Checking out an arbitrary ref there
would let unreviewed code run while holding that token and poison cache entries
that privileged workflows later restore (CWE-349; CodeQL
``actions/cache-poisoning/*``).

The workflow defends against this by resolving track tokens through a ``case``
statement that assigns **literal** ``"main"`` / ``"develop"`` strings, and by
refusing any other branch with a hard ``exit 1``. CodeQL cannot see through a
shell ``case``, so it still reports the checkout steps — which means the only
thing standing between this repo and that finding being real is the allowlist
itself, with nothing asserting it stays intact.

These tests are that assertion. They fail if someone widens the allowlist,
drops the deny branch, or lets attacker-influenced text reach a ``ref:``.
"""

import re
from pathlib import Path

import pytest
import yaml

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# The only branches a privileged nightly run may check out. Both are
# protected/PR-only, so anything running from them has been reviewed.
ALLOWED_REFS = {"main", "develop"}

# `<name>_ref="<value>"` assignments in the resolve-tracks shell step.
_REF_ASSIGNMENT = re.compile(r'^\s*(\w*_ref)="([^"]*)"', re.MULTILINE)


@pytest.fixture(scope="module")
def nightly_text() -> str:
    assert NIGHTLY.is_file(), f"missing workflow: {NIGHTLY}"
    return NIGHTLY.read_text()


@pytest.fixture(scope="module")
def nightly_yaml(nightly_text: str) -> dict:
    return yaml.safe_load(nightly_text)


def test_ref_assignments_are_allowlisted_literals(nightly_text: str) -> None:
    """Every `*_ref=` assignment is a literal branch name from the allowlist.

    The empty string is permitted: it is the "track not selected" initializer,
    and an unselected track's job never runs.
    """
    assignments = _REF_ASSIGNMENT.findall(nightly_text)
    assert assignments, "found no *_ref= assignments — has the parser moved?"

    offenders = {
        f"{name}={value!r}"
        for name, value in assignments
        if value != "" and value not in ALLOWED_REFS
    }
    assert not offenders, (
        "nightly.yml assigns a ref outside the allowlist "
        f"{sorted(ALLOWED_REFS)}: {sorted(offenders)}. "
        "A privileged nightly run must only check out reviewed branches — see "
        "the security note at the top of nightly.yml."
    )


def test_no_ref_is_interpolated_from_a_track_token(nightly_text: str) -> None:
    """Refs are assigned as literals, never sliced out of the track token.

    A parser that did `ref="${token#test-backend-}"` would pass the allowlist
    test above (no literal to see) while feeding attacker-influenced text
    straight to `ref:`.
    """
    interpolated = [
        line.strip()
        for line in nightly_text.splitlines()
        if re.search(r'^\s*\w*_ref="?\$', line)
    ]
    assert not interpolated, (
        "nightly.yml derives a ref from a shell expansion rather than a "
        f"literal: {interpolated}. Keep refs literal — see nightly.yml's "
        "security note."
    )


def test_unknown_branch_is_rejected_not_ignored(nightly_text: str) -> None:
    """A track naming a non-allowlisted branch fails the run.

    Falling through to the `*)` warning branch instead would leave the track
    silently unselected — safe today, but it removes the signal that tells an
    operator why their branch did not run, and invites "just add a default".
    """
    deny_arm = re.search(
        r"test-backend-\*\|.*?;;",
        nightly_text,
        re.DOTALL,
    )
    assert deny_arm, "the wildcard deny arm for unknown branches is gone"
    assert "exit 1" in deny_arm.group(0), (
        "the wildcard branch arm no longer fails the run; an unreviewed "
        "branch name must be rejected, not warned about"
    )


def test_checkout_refs_come_only_from_the_resolver(nightly_yaml: dict) -> None:
    """No checkout step reads a ref straight from workflow inputs or event data.

    The resolver is the choke point; a `ref:` naming `github.event.*` or
    `inputs.*` would route around it.
    """
    forbidden = ("github.event", "inputs.", "github.head_ref")
    offenders: list[str] = []

    for job_name, job in (nightly_yaml.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            uses = str(step.get("uses", ""))
            if "actions/checkout" not in uses:
                continue
            ref = str((step.get("with") or {}).get("ref", ""))
            if any(token in ref for token in forbidden):
                offenders.append(f"{job_name}: ref={ref!r}")

    assert not offenders, (
        "a nightly checkout takes its ref from untrusted input rather than "
        f"the resolve-tracks allowlist: {offenders}"
    )
