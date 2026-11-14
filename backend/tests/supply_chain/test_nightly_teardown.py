"""
Nightly teardown against log delivery that writes after the buckets are emptied.

``scripts/nightly/teardown.sh`` empties every nightly bucket once, then deletes
the stack. The ALB keeps delivering access logs (every five minutes) until
CloudFormation deletes it, so an object can land after that first pass. The
bucket's delete then fails with "bucket not empty". On 2026-11-04 the script
retried the delete three times without emptying the bucket again, retained it,
asked Cloud Control to delete it (refused for the same reason) and left
``nightly-develop-alb-access-logs`` standing. Every nightly after it failed
early validation creating a bucket of that name.

These tests run the **real script** with a stub ``aws`` on ``PATH`` that models
one stack: its buckets, their objects, a log writer that lands an object at
chosen delete attempts, CloudFormation's delete and retain, and Cloud Control's
delete. What matters is that no bucket survives a run, which only a run shows.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEARDOWN_SCRIPT = REPO_ROOT / "scripts" / "nightly" / "teardown.sh"

PREFIX = "nightly-develop"
STACK = f"{PREFIX}-PlatformStack"
REGION = "us-west-2"
ALB_BUCKET = f"{PREFIX}-alb-access-logs"
ALB_BUCKET_ID = "AlbAlbAccessLogBucketA14538BD"
UPLOADS_BUCKET = f"{PREFIX}-user-file-uploads-123456789012"
UPLOADS_BUCKET_ID = "FileUploadBucket1234ABCD"

#: Every case finishes in about a second. Anything near this limit is a runaway
#: loop, and the harness turns it into a failed assertion.
SCRIPT_TIMEOUT_SECONDS = 60

#: The stub CLI. Reads its world from ``TEARDOWN_STUB_STATE``, writes it back
#: after each call, and appends one line per call to ``TEARDOWN_STUB_CALLS``.
#: It prints what the script's ``--query`` would produce rather than evaluating
#: JMESPath, so each branch answers exactly one query the script makes.
_STUB = textwrap.dedent(
    '''
    #!/usr/bin/env python3
    import json, os, sys

    state_path = os.environ["TEARDOWN_STUB_STATE"]
    with open(state_path) as fh:
        state = json.load(fh)

    argv = sys.argv[1:]
    with open(os.environ["TEARDOWN_STUB_CALLS"], "a") as fh:
        fh.write(" ".join(argv) + "\\n")

    def arg(name, default=""):
        return argv[argv.index(name) + 1] if name in argv else default

    def save():
        with open(state_path, "w") as fh:
            json.dump(state, fh)

    service = argv[0] if argv else ""
    command = argv[1] if len(argv) > 1 else ""
    query = arg("--query")
    stack = state["stack"]
    objects = state["objects"]  # bucket -> [keys]; absent means no bucket

    def bucket_of_uri(uri):
        return uri[len("s3://"):].split("/", 1)[0]

    # ── S3 ────────────────────────────────────────────────────────────────
    if service == "s3api" and command == "list-buckets":
        print("\\t".join(sorted(objects)))
        sys.exit(0)
    if service == "s3api" and command == "head-bucket":
        sys.exit(0 if arg("--bucket") in objects else 254)
    if service == "s3api" and command == "list-object-versions":
        keys = objects.get(arg("--bucket"), [])
        if "DeleteMarkers" in query:
            print(json.dumps({"Objects": None}))
        else:
            print(json.dumps({"Objects": [{"Key": k, "VersionId": "v1"} for k in keys] or None}))
        sys.exit(0)
    if service == "s3api" and command == "delete-object":
        bucket = arg("--bucket")
        if arg("--key") in objects.get(bucket, []):
            objects[bucket].remove(arg("--key"))
        save()
        sys.exit(0)
    if service == "s3" and command == "rm":
        bucket = bucket_of_uri(argv[2])
        if bucket in objects:
            objects[bucket] = []
        save()
        sys.exit(0)
    if service == "s3vectors":
        print("[]")
        sys.exit(0)
    if service == "secretsmanager" or service == "logs":
        print("{}")
        sys.exit(0)

    # ── CloudFormation ────────────────────────────────────────────────────
    if service == "cloudformation":
        if command == "describe-stacks":
            if not stack["exists"]:
                sys.stderr.write("Stack with id " + STACK + " does not exist\\n")
                sys.exit(254)
            if query == "Stacks[0].StackName":
                print(STACK)
            elif query == "Stacks[0].StackStatus":
                print(stack["status"])
            else:
                print(stack["status"] + "\\treason")
            sys.exit(0)
        if command == "delete-stack":
            retain = []
            if "--retain-resources" in argv:
                i = argv.index("--retain-resources") + 1
                while i < len(argv) and not argv[i].startswith("--"):
                    retain.append(argv[i])
                    i += 1
            state["deleteCalls"] = state.get("deleteCalls", 0) + 1
            # The log writer: the ALB is deleted before the bucket it logs
            # to, and its last delivery can land in between.
            for bucket in state.get("lateWriteOn", {}).get(str(state["deleteCalls"]), []):
                if bucket in objects:
                    objects[bucket].append("late-%d.log.gz" % state["deleteCalls"])
            failed = False
            for lid, res in stack["resources"].items():
                if res["status"] == "DELETE_COMPLETE":
                    continue
                if lid in retain:
                    res["status"] = "DELETE_COMPLETE"
                    continue  # retained: the stack forgets it, the bucket stays
                if res["type"] == "AWS::S3::Bucket" and objects.get(res["physical"]):
                    res["status"] = "DELETE_FAILED"
                    failed = True
                    continue
                res["status"] = "DELETE_COMPLETE"
                objects.pop(res["physical"], None)
            stack["status"] = "DELETE_FAILED" if failed else "DELETE_COMPLETE"
            stack["exists"] = failed
            save()
            sys.exit(0)
        if command == "wait":
            sys.exit(0 if not stack["exists"] else 255)
        if command == "describe-stack-events":
            print("events")
            sys.exit(0)
        if command == "list-stack-resources":
            failed = [(lid, r) for lid, r in stack["resources"].items() if r["status"] == "DELETE_FAILED"]
            if "PhysicalResourceId" in query:
                print("\\t".join(r["physical"] for _, r in failed if r["type"] == "AWS::S3::Bucket"))
            else:
                print("\\t".join(lid for lid, _ in failed))
            sys.exit(0)
        if command == "describe-stack-resource":
            res = stack["resources"][arg("--logical-resource-id")]
            print(res["type"] + "\\t" + res["physical"])
            sys.exit(0)

    # ── Cloud Control ─────────────────────────────────────────────────────
    if service == "cloudcontrol":
        if command == "delete-resource":
            ident = arg("--identifier")
            token = "tok-" + ident
            if objects.get(ident):
                state.setdefault("requests", {})[token] = "FAILED"
            else:
                objects.pop(ident, None)
                state.setdefault("requests", {})[token] = "SUCCESS"
            save()
            print(token)
            sys.exit(0)
        if command == "get-resource-request-status":
            print(state["requests"][arg("--request-token")])
            sys.exit(0)

    sys.stderr.write("unexpected aws invocation: " + " ".join(argv) + "\\n")
    sys.exit(2)
    '''
).strip().replace("STACK", repr(STACK))


def _bucket(physical: str) -> dict:
    return {"type": "AWS::S3::Bucket", "physical": physical, "status": "CREATE_COMPLETE"}


@pytest.fixture
def run_teardown(tmp_path):
    """Run ``teardown.sh`` against a scripted stack. Returns (result, state, calls).

    ``late_write_on`` maps a delete-stack call number (1-based) to the buckets
    the log writer drops an object into just before that call deletes them.
    """
    if sys.platform == "win32":
        pytest.skip("teardown.sh harness needs a POSIX shell environment")

    def _run(late_write_on=None, script=TEARDOWN_SCRIPT):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "aws"
        stub.write_text(_STUB)
        stub.chmod(0o755)

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "stack": {
                        "exists": True,
                        "status": "CREATE_COMPLETE",
                        "resources": {
                            ALB_BUCKET_ID: _bucket(ALB_BUCKET),
                            UPLOADS_BUCKET_ID: _bucket(UPLOADS_BUCKET),
                            "Alb": {
                                "type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                                "physical": "arn:alb",
                                "status": "CREATE_COMPLETE",
                            },
                        },
                    },
                    "objects": {
                        ALB_BUCKET: ["AWSLogs/early.log.gz"],
                        UPLOADS_BUCKET: ["u-1/file.pdf"],
                    },
                    "lateWriteOn": {str(k): v for k, v in (late_write_on or {}).items()},
                }
            )
        )
        calls_path = tmp_path / "calls.txt"
        calls_path.write_text("")

        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "TEARDOWN_STUB_STATE": str(state_path),
            "TEARDOWN_STUB_CALLS": str(calls_path),
            "CDK_PROJECT_PREFIX": PREFIX,
            "CDK_AWS_REGION": REGION,
            "TEARDOWN_RETRY_DELAY_SECONDS": "0",
        }
        try:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=SCRIPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as expired:
            raise AssertionError(
                f"teardown.sh did not terminate within {SCRIPT_TIMEOUT_SECONDS}s"
            ) from expired

        state = json.loads(state_path.read_text())
        calls = [line for line in calls_path.read_text().splitlines() if line.strip()]
        return result, state, calls

    return _run


def _delete_stack_calls(calls):
    return [c for c in calls if c.startswith("cloudformation delete-stack")]


def _emptied(calls, bucket):
    return [c for c in calls if c == f"s3 rm s3://{bucket} --recursive"]


class TestNoBucketOutlivesTheTeardown:
    def test_a_quiet_stack_deletes_once_and_empties_nothing_twice(self, run_teardown):
        result, state, calls = run_teardown()

        assert result.returncode == 0, result.stdout + result.stderr
        assert state["objects"] == {}
        assert len(_delete_stack_calls(calls)) == 1
        assert len(_emptied(calls, ALB_BUCKET)) == 1

    def test_a_log_that_lands_after_the_first_pass_is_emptied_before_the_retry(
        self, run_teardown
    ):
        """The 2026-11-04 shape: the ALB's last delivery lands between the
        up-front empty and the bucket's delete."""
        result, state, calls = run_teardown(late_write_on={1: [ALB_BUCKET]})

        assert result.returncode == 0, result.stdout + result.stderr
        assert ALB_BUCKET not in state["objects"], "the access-log bucket survived teardown"
        assert not state["stack"]["exists"]
        deletes = _delete_stack_calls(calls)
        assert len(deletes) == 2
        assert all("--retain-resources" not in c for c in deletes)
        # Only the bucket that refused is emptied again; the rest are gone.
        assert len(_emptied(calls, ALB_BUCKET)) == 2
        assert len(_emptied(calls, UPLOADS_BUCKET)) == 1

    def test_a_bucket_still_refusing_after_every_retry_is_emptied_before_cloud_control(
        self, run_teardown
    ):
        """A writer that keeps landing objects until the last attempt pushes the
        bucket to the retain path, where Cloud Control must find it empty."""
        result, state, calls = run_teardown(
            late_write_on={1: [ALB_BUCKET], 2: [ALB_BUCKET], 3: [ALB_BUCKET]}
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert ALB_BUCKET not in state["objects"], "the retained bucket was left standing"
        assert not state["stack"]["exists"]
        retain_calls = [c for c in _delete_stack_calls(calls) if "--retain-resources" in c]
        assert retain_calls == [
            f"cloudformation delete-stack --stack-name {STACK} --region {REGION} "
            f"--retain-resources {ALB_BUCKET_ID}"
        ]
        cloud_control = [c for c in calls if c.startswith("cloudcontrol delete-resource")]
        assert len(cloud_control) == 1
        rm_before_cc = calls.index(cloud_control[0]) - 1
        assert calls[rm_before_cc] == f"s3 rm s3://{ALB_BUCKET} --recursive"
