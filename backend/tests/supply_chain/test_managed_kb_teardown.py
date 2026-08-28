"""
Teardown of runtime-created knowledge bases.

Requirement 24.9. Managed knowledge bases are created by the provisioning saga at
runtime, so they are not CloudFormation children and ``delete-stack`` does not touch
them. Left behind they keep billing at $5.00/GB-month while being invisible in the
CloudFormation console — a leak with no console to notice it in.

These tests run the **real script** with a stub ``aws`` on ``PATH``. Static analysis
of the file could confirm that a tag filter is written; it could not confirm that an
untagged knowledge base survives a run, which is the assertion that matters. The
stub is a small Python program that answers ``list-knowledge-bases``,
``list-tags-for-resource`` and ``delete-knowledge-base`` from a JSON fixture and
records every call, so the script's behaviour is observable without an AWS account.

Two properties carry the risk:

* **Only tagged resources are deleted.** Two environments share an account, so a
  filter that matched by name prefix — or that treated unreadable tags as ours —
  would delete another environment's corpus.
* **The service role outlives its knowledge bases.** Bedrock needs the role to
  perform the delete, so the script must fail *before* any stack comes down when a
  knowledge base is not confirmed absent. ``DELETE_UNSUCCESSFUL`` is a real terminal
  state that retrying does not clear.

Feature: managed-kb-migration
Requirements: 20.8, 13.4, 13.5, 24.9
"""

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from apis.shared.kb_backend import tags as kb_tags

REPO_ROOT = Path(__file__).resolve().parents[3]
TEARDOWN_DIR = REPO_ROOT / "scripts" / "teardown"
KB_SCRIPT = TEARDOWN_DIR / "managed-kb.sh"
DESTROY_SCRIPT = TEARDOWN_DIR / "destroy.sh"

PREFIX = "testprefix"
ENVIRONMENT = "dev"
REGION = "us-west-2"
ACCOUNT = "123456789012"

#: Every case here finishes in well under a second. Anything approaching this
#: limit is a runaway loop, and the harness turns it into a failed assertion
#: rather than a blocked suite.
SCRIPT_TIMEOUT_SECONDS = 30

#: The stub CLI. Reads its scripted world from ``KB_STUB_STATE`` and appends one
#: line per call to ``KB_STUB_CALLS``, so a test can assert on what was *not*
#: called as easily as on what was.
_STUB = textwrap.dedent(
    '''
    #!/usr/bin/env python3
    import json, os, sys

    state_path = os.environ["KB_STUB_STATE"]
    calls_path = os.environ["KB_STUB_CALLS"]

    with open(state_path) as fh:
        state = json.load(fh)

    argv = sys.argv[1:]
    with open(calls_path, "a") as fh:
        fh.write(" ".join(argv) + "\\n")

    def arg(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    command = argv[1] if len(argv) > 1 else ""

    if command == "list-knowledge-bases":
        # Optional: start failing after N list calls, which is what an
        # unreadable account looks like mid-teardown.
        fail_after = os.environ.get("KB_STUB_FAIL_LIST_AFTER")
        state["listCalls"] = state.get("listCalls", 0) + 1
        if fail_after and state["listCalls"] > int(fail_after):
            with open(state_path, "w") as fh:
                json.dump(state, fh)
            sys.stderr.write("ThrottlingException\\n")
            sys.exit(254)

        # Each call pops one "view" of the account, so a test can script a
        # knowledge base disappearing after its delete.
        views = state["views"]
        view = views[0] if len(views) == 1 else views.pop(0)
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        print(json.dumps({"knowledgeBaseSummaries": view}))
        sys.exit(0)

    if command == "list-tags-for-resource":
        resource_arn = arg("--resource-arn", "")
        kb_id = resource_arn.rsplit("/", 1)[-1]
        if kb_id in state.get("tagErrors", []):
            sys.stderr.write("AccessDeniedException\\n")
            sys.exit(254)
        print(json.dumps({"tags": state.get("tags", {}).get(kb_id, {})}))
        sys.exit(0)

    if command == "delete-knowledge-base":
        kb_id = arg("--knowledge-base-id", "")
        if kb_id in state.get("deleteErrors", []):
            sys.stderr.write("ValidationException\\n")
            sys.exit(254)
        print(json.dumps({"knowledgeBaseId": kb_id, "status": "DELETING"}))
        sys.exit(0)

    sys.stderr.write("unexpected aws invocation: " + " ".join(argv) + "\\n")
    sys.exit(2)
    '''
).strip()


def _kb(kb_id: str, status: str = "ACTIVE"):
    return {"knowledgeBaseId": kb_id, "name": f"{PREFIX}-kb-{kb_id}", "status": status}


def _ours():
    """Built through the canonical helper rather than spelled out.

    A fixture that hardcodes tag keys keeps passing after the keys change under
    it — which is exactly how the writer, the reconciler, the construct and this
    script came to disagree three ways.
    """
    return kb_tags.build_tags("ast-1", "u-1", PREFIX, ENVIRONMENT)


@pytest.fixture
def run_teardown(tmp_path):
    """Run ``managed-kb.sh`` against a scripted account. Returns (result, calls)."""

    def _run(views, tags, tag_errors=None, delete_errors=None, extra_env=None, script=KB_SCRIPT):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "aws"
        stub.write_text(_STUB)
        stub.chmod(0o755)

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "views": views,
                    "tags": tags,
                    "tagErrors": tag_errors or [],
                    "deleteErrors": delete_errors or [],
                }
            )
        )
        calls_path = tmp_path / "calls.txt"
        calls_path.write_text("")

        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "KB_STUB_STATE": str(state_path),
            "KB_STUB_CALLS": str(calls_path),
            # Supplied directly so load-env.sh is not needed: the script only
            # sources it when log_info is undefined, and these are the values it
            # would have exported.
            "CDK_AWS_REGION": REGION,
            "CDK_AWS_ACCOUNT": ACCOUNT,
            kb_tags.ENV_TAG_VALUE_PREFIX: PREFIX,
            kb_tags.ENV_TAG_VALUE_ENVIRONMENT: ENVIRONMENT,
            # Fast but never zero. An interval of 0 made the script's elapsed
            # counter stop advancing, so its timeout was never reached and the
            # loop span at full CPU issuing an `aws` call per iteration — this
            # test ran for sixteen hours before it was killed. The script now
            # clamps the interval to 1; the test no longer asks for 0 either,
            # because a test should not rely on a clamp to terminate.
            "KB_DELETE_POLL_TIMEOUT_SECONDS": "1",
            "KB_DELETE_POLL_INTERVAL_SECONDS": "1",
        }
        env.update(extra_env or {})

        # The script sources load-env.sh only if log_info is undefined, so define
        # the log helpers up front and dot-source the script into that shell.
        harness = textwrap.dedent(
            f"""
            log_info() {{ echo "[INFO] $1"; }}
            log_warn() {{ echo "[WARN] $1"; }}
            log_success() {{ echo "[SUCCESS] $1"; }}
            GREEN=""; NC=""
            source "{script}"
            """
        )
        # A hard wall. Every case here should finish in well under a second, so a
        # run that reaches this limit is a runaway loop and must fail the test
        # rather than block the suite. `timeout` raises, which is the correct
        # outcome: a hang is a defect, not a slow pass.
        try:
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=SCRIPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as expired:
            calls = [line for line in calls_path.read_text().splitlines() if line.strip()]
            raise AssertionError(
                f"managed-kb.sh did not terminate within {SCRIPT_TIMEOUT_SECONDS}s "
                f"— this is the infinite-poll shape, not slowness. "
                f"It made {len(calls)} AWS calls before being killed."
            ) from expired

        calls = [line for line in calls_path.read_text().splitlines() if line.strip()]
        return result, calls

    return _run


def _deleted_ids(calls):
    return [
        line.split("--knowledge-base-id ")[1].split()[0]
        for line in calls
        if "delete-knowledge-base" in line
    ]


# ── Only tagged resources ────────────────────────────────────────────────────
class TestOnlyTaggedResourcesAreDeleted:
    def test_ours_is_deleted_and_nothing_else_is(self, run_teardown):
        """The whole property in one run: one of ours, one from another
        environment, one from another project, and one with no tags at all."""
        views = [
            [_kb("KB-OURS"), _kb("KB-OTHERENV"), _kb("KB-OTHERPROJ"), _kb("KB-UNTAGGED")],
            [_kb("KB-OTHERENV"), _kb("KB-OTHERPROJ"), _kb("KB-UNTAGGED")],
        ]
        tags = {
            "KB-OURS": _ours(),
            "KB-OTHERENV": kb_tags.build_tags("ast-2", "u-2", PREFIX, "prod"),
            "KB-OTHERPROJ": kb_tags.build_tags("ast-3", "u-3", "someone-else", ENVIRONMENT),
            "KB-UNTAGGED": {},
        }

        result, calls = run_teardown(views, tags)

        assert result.returncode == 0, result.stdout + result.stderr
        assert _deleted_ids(calls) == ["KB-OURS"]

    def test_a_prefix_match_with_the_wrong_environment_survives(self, run_teardown):
        """Two environments share an account. A knowledge base carrying our
        project prefix but another environment's tag belongs to that
        environment, and its name looks exactly like ours."""
        views = [[_kb("KB-PROD")], [_kb("KB-PROD")]]
        tags = {"KB-PROD": kb_tags.build_tags("ast-p", "u-p", PREFIX, "prod")}

        result, calls = run_teardown(views, tags)

        assert result.returncode == 0
        assert _deleted_ids(calls) == []

    def test_unreadable_tags_mean_not_ours(self, run_teardown):
        """Unknown ownership is not ownership. Refusing to delete something we
        cannot attribute is the only safe direction for a destructive pass."""
        views = [[_kb("KB-OPAQUE")], [_kb("KB-OPAQUE")]]

        result, calls = run_teardown(views, {}, tag_errors=["KB-OPAQUE"])

        assert result.returncode == 0
        assert _deleted_ids(calls) == []

    def test_an_empty_account_is_a_clean_no_op(self, run_teardown):
        result, calls = run_teardown([[]], {})

        assert result.returncode == 0
        assert _deleted_ids(calls) == []
        assert "No Managed Knowledge Bases to delete" in result.stdout

    def test_pagination_is_followed(self, run_teardown):
        """A single-page walk would silently leave every knowledge base past the
        first page billing."""
        views = [
            [_kb("KB-A"), _kb("KB-B")],
            [_kb("KB-B")],
            [],
        ]
        tags = {"KB-A": _ours(), "KB-B": _ours()}

        result, calls = run_teardown(views, tags)

        assert result.returncode == 0, result.stdout + result.stderr
        assert sorted(_deleted_ids(calls)) == ["KB-A", "KB-B"]

    def test_the_scope_is_bounded(self, run_teardown):
        """A tag filter that suddenly matched everything costs at most one
        refusal rather than the account."""
        many = [_kb(f"KB-{i}") for i in range(5)]
        tags = {f"KB-{i}": _ours() for i in range(5)}

        result, calls = run_teardown([many, many], tags, extra_env={"KB_TEARDOWN_MAX": "2"})

        assert result.returncode == 1
        assert _deleted_ids(calls) == []
        assert "Refusing to delete" in result.stdout


# ── The role must outlive its knowledge bases ────────────────────────────────
class TestTheRoleOutlivesItsKnowledgeBases:
    def test_a_knowledge_base_that_never_disappears_fails_the_run(self, run_teardown):
        """Requirements 13.4, 13.5. "Delete call accepted" is not "resource
        gone": deletion is asynchronous and took 2-6 minutes when measured. A
        non-zero exit is what stops the caller deleting the service role out from
        under a knowledge base that still needs it."""
        views = [[_kb("KB-STUCK")]]  # one view, reused: it never disappears
        tags = {"KB-STUCK": _ours()}

        result, calls = run_teardown(views, tags)

        assert result.returncode == 1
        assert _deleted_ids(calls) == ["KB-STUCK"]
        assert "not confirmed absent" in result.stdout
        assert "must" in result.stdout and "outlive" in result.stdout

    def test_absence_is_confirmed_by_polling_the_list(self, run_teardown):
        """Not by the delete call's own return. The script must ask the account."""
        views = [[_kb("KB-GONE")], []]
        tags = {"KB-GONE": _ours()}

        result, calls = run_teardown(views, tags)

        assert result.returncode == 0
        assert "confirmed absent" in result.stdout
        # At least two list calls: one to find it, one to confirm it went.
        assert sum(1 for line in calls if "list-knowledge-bases" in line) >= 2

    def test_delete_unsuccessful_is_surfaced_with_its_remedy(self, run_teardown):
        """A real terminal state — the dev account has held one since
        2025-11-24 — that retrying does not clear. The message must name the
        remedy rather than time out generically."""
        views = [[_kb("KB-BROKEN", status="DELETE_UNSUCCESSFUL")]]
        tags = {"KB-BROKEN": _ours()}

        result, calls = run_teardown(views, tags)

        assert result.returncode == 1
        assert _deleted_ids(calls) == [], "retried a delete that cannot succeed"
        assert "DELETE_UNSUCCESSFUL" in result.stdout
        assert "dataDeletionPolicy" in result.stdout

    def test_a_failed_delete_call_does_not_report_success(self, run_teardown):
        views = [[_kb("KB-REFUSED")], [_kb("KB-REFUSED")]]
        tags = {"KB-REFUSED": _ours()}

        result, _ = run_teardown(views, tags, delete_errors=["KB-REFUSED"])

        assert result.returncode == 1

    def test_a_failed_discovery_listing_refuses_to_continue(self, run_teardown):
        """The very first listing fails, so the script never learns what exists.

        An unreadable account is indistinguishable from an empty one. Treating it
        as empty would report a clean teardown having deleted nothing, while the
        knowledge bases kept billing — and `done < <(list_knowledge_bases)` hides
        the lister's exit status entirely, which is how the first version of this
        script behaved.
        """
        result, calls = run_teardown(
            [[_kb("KB-A")]],
            {"KB-A": _ours()},
            extra_env={"KB_STUB_FAIL_LIST_AFTER": "0"},
        )

        assert result.returncode == 1
        assert _deleted_ids(calls) == []
        assert "Refusing to continue" in result.stdout
        assert "No Managed Knowledge Bases to delete" not in result.stdout, (
            "an unreadable account was reported as an empty one"
        )

    def test_an_unlistable_account_is_not_read_as_confirmed_absence(self, run_teardown):
        """Fail safe on the *confirmation* path. The presence check runs out of
        scripted views and the stub then errors, which is what an unreadable
        account looks like mid-teardown. Answering "absent" there would report a
        successful teardown and let the caller delete the service role out from
        under a live knowledge base.
        """
        # Two views: one to discover and delete, then the stub runs dry and fails.
        views = [[_kb("KB-MAYBE")]]
        tags = {"KB-MAYBE": _ours()}

        result, _ = run_teardown(
            views,
            tags,
            extra_env={"KB_STUB_FAIL_LIST_AFTER": "2"},
        )

        assert result.returncode == 1
        # The success line specifically, not the substring — "were not confirmed
        # absent" contains it and the first version of this assertion matched that.
        assert "KB-MAYBE confirmed absent" not in result.stdout
        assert "not confirmed absent" in result.stdout


# ── Ordering inside destroy.sh ───────────────────────────────────────────────
class TestTeardownOrdering:
    """Asserted statically, because running ``destroy.sh`` would delete stacks.

    The property is textual position: knowledge bases must be handled before the
    first stack delete, and the failure must abort rather than continue.
    """

    def test_knowledge_bases_are_torn_down_before_any_stack(self):
        """Line-based, and comparing against the first *invocation*.

        Two traps, both of which this test originally fell into. Searching the raw
        text for ``aws cloudformation delete-stack`` matches the header comment
        that explains why the script uses that command instead of ``cdk destroy``
        — a match hundreds of lines above any code. And ``destroy_stack`` appears
        first as a function *definition*, which executes nothing.

        So: skip comments, and find where ``destroy_stack`` is *called*.
        """
        lines = DESTROY_SCRIPT.read_text().splitlines()

        def first_line_matching(predicate) -> int:
            for index, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if predicate(stripped):
                    return index
            return -1

        kb_phase = first_line_matching(lambda s: "managed-kb.sh" in s)
        first_call = first_line_matching(
            lambda s: (s.startswith("destroy_stack") or s.startswith("if destroy_stack"))
            and not s.startswith("destroy_stack()")
        )

        assert kb_phase != -1, "destroy.sh never invokes managed-kb.sh"
        assert first_call != -1, "no destroy_stack invocation found; has the script changed shape?"
        assert kb_phase < first_call, (
            f"destroy.sh calls destroy_stack at line {first_call + 1} before tearing "
            f"down knowledge bases at line {kb_phase + 1}; the Bedrock service role "
            f"lives in PlatformStack and its knowledge bases cannot be deleted "
            f"without it"
        )

    def test_a_knowledge_base_failure_aborts_the_teardown(self):
        """Rather than being logged and stepped over. Leaving paid resources
        behind is the one outcome worse than a teardown that stops and says why.

        Asserted on the *shape* of the invocation, not on the presence of the
        strings ``managed-kb.sh`` and ``exit 1`` somewhere in the block — both
        survive a mutation that changes the condition to ``if false`` and adds
        ``|| true``, which is exactly the regression this is meant to catch.
        """
        lines = [line.strip() for line in DESTROY_SCRIPT.read_text().splitlines()]
        invocations = [line for line in lines if "managed-kb.sh" in line and not line.startswith("#")]

        assert invocations, "destroy.sh never invokes managed-kb.sh"
        assert len(invocations) == 1, f"invoked more than once: {invocations}"

        invocation = invocations[0]
        assert invocation.startswith("if ! bash"), (
            f"the knowledge base teardown is not invoked in a failing condition: "
            f"{invocation!r}. A bare call, or one ending in `|| true`, means a "
            f"teardown that could not delete a knowledge base proceeds to delete "
            f"the service role it needs."
        )
        assert "|| true" not in invocation
        assert "exit 1" in DESTROY_SCRIPT.read_text()[DESTROY_SCRIPT.read_text().index(invocation):]

    def test_the_kb_script_is_executable_and_strict(self):
        body = KB_SCRIPT.read_text()
        assert body.startswith("#!/bin/bash")
        assert "set -euo pipefail" in body


class TestTheWaitLoopCannotRunAway:
    """Regression tests for a script that once ran for sixteen hours.

    The wait loop advanced a counter by the poll interval and stopped when the
    counter reached the timeout. At an interval of ``0`` the counter never
    advanced, so the timeout was never reached — an unbounded loop issuing an
    ``aws`` call and a ``python3`` parse per iteration, at full CPU. It was a test
    that set the interval to 0 to run quickly that found it, by not finishing.
    """

    def test_a_zero_poll_interval_still_terminates(self, run_teardown):
        """The exact configuration that hung. It must now be clamped."""
        views = [[_kb("KB-STUCK")]]  # never disappears, so the loop runs to its bound
        tags = {"KB-STUCK": _ours()}

        result, _ = run_teardown(
            views,
            tags,
            extra_env={
                "KB_DELETE_POLL_INTERVAL_SECONDS": "0",
                "KB_DELETE_POLL_TIMEOUT_SECONDS": "1",
            },
        )

        assert result.returncode == 1
        assert "not confirmed deleted" in result.stdout

    def test_a_non_numeric_tunable_does_not_abort_the_teardown(self, run_teardown):
        """``[ abc -lt 1 ]`` fails under ``set -e``. A typo in a tunable must not
        take down a teardown that is otherwise fine.

        Asserted on **stderr** as well as the exit code, because the numeric-check
        `|| fallback` clamps the value either way — so termination alone passes with
        the type guard removed. What the guard actually buys is that bash never
        evaluates ``[ ten -ge 1 ]``, and that comparison announces itself with
        "integer expression expected".
        """
        views = [[_kb("KB-GONE")], []]
        tags = {"KB-GONE": _ours()}

        result, _ = run_teardown(
            views,
            tags,
            extra_env={"KB_DELETE_POLL_INTERVAL_SECONDS": "ten"},
        )

        assert result.returncode == 0
        assert "confirmed absent" in result.stdout
        assert "integer expression expected" not in result.stderr, (
            "a non-numeric tunable reached a numeric comparison; the type guard is "
            f"not catching it. stderr: {result.stderr}"
        )

    def test_the_loop_is_bounded_by_attempts_not_only_by_arithmetic(self):
        """Belt and braces, asserted in the source: termination must not depend
        solely on a counter that a later edit could stop advancing."""
        body = KB_SCRIPT.read_text()
        assert "ATTEMPTS_LEFT" in body
        assert 'while [ "${ATTEMPTS_LEFT}" -gt 0 ]' in body

    def test_the_polling_bound_is_at_least_the_measured_deletion_time(self):
        """Deletion took 2-6 minutes when measured, so the default tolerance must
        comfortably exceed it. Pinned as a literal: this is a property of AWS's
        behaviour, not a knob."""
        body = KB_SCRIPT.read_text()
        assert "KB_DELETE_POLL_TIMEOUT_SECONDS:-480}" in body
