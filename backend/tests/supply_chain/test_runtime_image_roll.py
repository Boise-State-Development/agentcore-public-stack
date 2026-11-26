"""The AgentCore Runtime image roll (scripts/build/deploy-runtime-image-if-changed.sh).

`UpdateAgentRuntime` is a full replacement: a field the update leaves out goes
back to the service default. For `hooksConfiguration` (the Runtime V2 build
warmup hook, docs/specs/agentcore-runtime-v2.md §5b) the default is "no
hooks", and the update still succeeds, so a roll that drops it is invisible
until first turns in new sessions get slower. `platformVersion` has the same
shape (spec §3 B1).

These tests run the real script against a fake `aws` on PATH that keeps one
Runtime's state in a JSON file and applies an update the way the service does:
whatever the payload omits is gone afterwards. The fake can also play a CLI
whose service model predates a field, which neither prints it from
`get-agent-runtime` nor lists it in the update skeleton.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "build" / "deploy-runtime-image-if-changed.sh"
WRAPPER = REPO_ROOT / "scripts" / "build" / "deploy-runtime-image-one.sh"

REPO_URI = "123456789012.dkr.ecr.us-west-2.amazonaws.com/test-inference-api"
OLD_URI = f"{REPO_URI}:old"
NEW_URI = f"{REPO_URI}:new"
WARMUP_HOOKS = {"build": {"warmup": {"action": {"httpPost": {"path": "/warmup"}}}}}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_AWS = r'''#!/usr/bin/env python3
"""A stand-in for the AWS CLI: one Runtime, kept in $FAKE_AWS_STATE."""
import json
import os
import sys

UPDATE_FIELDS = [
    "agentRuntimeId", "agentRuntimeArtifact", "roleArn", "networkConfiguration",
    "description", "authorizerConfiguration", "requestHeaderConfiguration",
    "protocolConfiguration", "lifecycleConfiguration", "metadataConfiguration",
    "environmentVariables", "filesystemConfigurations", "capacityProviderConfiguration",
    "platformVersion", "hooksConfiguration", "clientToken",
]
READ_ONLY = ["agentRuntimeArn", "agentRuntimeName", "agentRuntimeVersion", "status",
             "createdAt", "lastUpdatedAt", "workloadIdentityDetails", "failureReason"]

path = os.environ["FAKE_AWS_STATE"]
with open(path) as fh:
    state = json.load(fh)
args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def save():
    with open(path, "w") as fh:
        json.dump(state, fh)


def known(field):
    return field not in state.get("cli_unknown_fields", [])


if args[:1] == ["--version"]:
    print("aws-cli/2.0.0 fake")
elif args[:2] == ["ssm", "get-parameter"]:
    print(state["ssm"][opt("--name")])
elif args[:2] == ["ecr", "describe-images"]:
    sys.exit(254)
elif args[:2] == ["ecr", "batch-get-image"]:
    # One image config per tag; "unreadable" plays a repository this role cannot inspect.
    tag = opt("--image-ids").split("=", 1)[1]
    labels = state.get("image_labels", {}).get(tag)
    if labels == "unreadable" or labels is None:
        sys.exit(254)
    print(json.dumps({"config": {"digest": "sha256:config-" + tag}}))
elif args[:2] == ["ecr", "get-download-url-for-layer"]:
    print("https://ecr.invalid/" + opt("--layer-digest").split("config-", 1)[1])
elif args[:2] == ["bedrock-agentcore-control", "get-agent-runtime"]:
    runtime = {k: v for k, v in state["runtime"].items() if known(k)}
    query = opt("--query")
    if query:
        print(runtime.get(query.strip("'"), "None") or "None")
    else:
        print(json.dumps(runtime))
elif args[:2] == ["bedrock-agentcore-control", "update-agent-runtime"]:
    if "--generate-cli-skeleton" in args:
        print(json.dumps({k: "" for k in UPDATE_FIELDS if known(k)}))
        sys.exit(0)
    with open(opt("--cli-input-json").replace("file://", "", 1)) as fh:
        payload = json.load(fh)
    state.setdefault("updates", []).append(payload)
    # Full replacement: read-only fields stay, everything else is the payload.
    runtime = {k: v for k, v in state["runtime"].items() if k in READ_ONLY}
    runtime.update(payload)
    for field in state.get("service_drops", []):
        runtime.pop(field, None)
    runtime.update(state.get("service_overrides", {}))
    runtime["status"] = state.get("status_after_update", "READY")
    runtime.pop("failureReason", None)
    if runtime["status"] != "READY":
        runtime["failureReason"] = state.get("failure_reason_after_update", "A lifecycle hook failed.")
    state["runtime"] = runtime
    save()
    print(runtime["agentRuntimeArn"])
else:
    sys.stderr.write("fake aws: unexpected call %r\n" % (args,))
    sys.exit(99)
'''


FAKE_CURL = r'''#!/usr/bin/env python3
"""A stand-in for curl: serves the image config the fake ECR pointed at."""
import json
import os
import sys

with open(os.environ["FAKE_AWS_STATE"]) as fh:
    state = json.load(fh)
tag = sys.argv[-1].rsplit("/", 1)[1]
print(json.dumps({"config": {"Labels": state["image_labels"][tag]}}))
'''

HOOK_LABEL = {"org.agentcore-public-stack.runtime-warmup-hook": "warmup-v1"}


def _runtime(**overrides: Any) -> Dict[str, Any]:
    runtime: Dict[str, Any] = {
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1",
        "agentRuntimeId": "rt-1",
        "agentRuntimeName": "test_runtime",
        "agentRuntimeVersion": "7",
        "status": "READY",
        "roleArn": "arn:aws:iam::123456789012:role/runtime",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": {"A": "1"},
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": OLD_URI}},
        "platformVersion": "V2",
        "hooksConfiguration": WARMUP_HOOKS,
    }
    runtime.update(overrides)
    return {k: v for k, v in runtime.items() if v is not None}


class Roll:
    """One run of the script against a fake Runtime."""

    def __init__(self, tmp_path: Path, runtime: Dict[str, Any], **state: Any) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "aws"
        fake.write_text(FAKE_AWS.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(FAKE_CURL.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "ssm": {"/test/image-tag": NEW_URI, "/test/runtime-id": "rt-1"},
                    "runtime": runtime,
                    # The new image serves the hook unless a test says otherwise.
                    "image_labels": {"new": HOOK_LABEL},
                    **state,
                }
            )
        )
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AWS_STATE": str(self.state_path),
            "AWS_REGION": "us-west-2",
        }
        self.result: Optional[subprocess.CompletedProcess] = None

    def run(self, *extra: str) -> "Roll":
        self.result = subprocess.run(
            [
                "bash", str(SCRIPT),
                "--service", "inference-api",
                "--runtime-id-ssm", "/test/runtime-id",
                "--image-uri-ssm", "/test/image-tag",
                "--ecr-repo-uri", REPO_URI,
                *extra,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return self

    @property
    def state(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text())

    @property
    def updates(self) -> List[Dict[str, Any]]:
        return self.state.get("updates", [])

    @property
    def code(self) -> int:
        assert self.result is not None
        return self.result.returncode

    @property
    def log(self) -> str:
        assert self.result is not None
        return self.result.stderr


class TestHooksSurviveTheRoll:
    def test_the_update_resends_the_hooks_and_the_runtime_keeps_them(self, tmp_path):
        roll = Roll(tmp_path, _runtime()).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert len(roll.updates) == 1
        assert roll.updates[0]["hooksConfiguration"] == WARMUP_HOOKS
        assert roll.state["runtime"]["hooksConfiguration"] == WARMUP_HOOKS
        assert roll.state["runtime"]["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": NEW_URI}}
        assert "Lifecycle hooks unchanged" in roll.log
        assert roll.result.stdout.strip().splitlines()[-1] == "new"

    def test_hooks_are_carried_without_the_expectation_too(self, tmp_path):
        """The switch only decides whether an old CLI is refused. Hooks on the
        runtime are carried forward whoever put them there."""
        roll = Roll(tmp_path, _runtime()).run()

        assert roll.code == 0, roll.log
        assert roll.updates[0]["hooksConfiguration"] == WARMUP_HOOKS

    def test_hooks_that_changed_during_the_update_fail_the_job(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), service_drops=["hooksConfiguration"]).run("--expect-warmup-hook")

        assert roll.code == 9
        assert "Lifecycle hooks changed during the image update" in roll.log

    def test_a_runtime_without_hooks_stays_without_them(self, tmp_path):
        roll = Roll(tmp_path, _runtime(hooksConfiguration=None)).run()

        assert roll.code == 0, roll.log
        assert "hooksConfiguration" not in roll.updates[0]
        assert "Lifecycle hooks unchanged: <none>" in roll.log

    def test_the_switch_on_but_no_hooks_yet_warns_and_never_adds_one(self, tmp_path):
        """CloudFormation configures the hook. An image roll that ran first
        must not invent it."""
        roll = Roll(tmp_path, _runtime(hooksConfiguration=None)).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert "hooksConfiguration" not in roll.updates[0]
        assert "the runtime has no hooksConfiguration" in roll.log


class TestACliThatCannotSeeHooks:
    def test_is_refused_when_the_deployment_has_the_hook_on(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), cli_unknown_fields=["hooksConfiguration"]).run("--expect-warmup-hook")

        assert roll.code == 7
        assert roll.updates == [], "the update must not be sent: it would remove the hook"
        assert "does not know update-agent-runtime's hooksConfiguration" in roll.log
        assert roll.state["runtime"]["hooksConfiguration"] == WARMUP_HOOKS

    def test_still_rolls_a_deployment_that_has_the_hook_off(self, tmp_path):
        roll = Roll(
            tmp_path, _runtime(hooksConfiguration=None), cli_unknown_fields=["hooksConfiguration"]
        ).run()

        assert roll.code == 0, roll.log
        assert len(roll.updates) == 1
        assert "cannot be seen or carried" in roll.log


class TestAFailedHook:
    def test_an_update_that_fails_reports_why_and_where_to_look(self, tmp_path):
        roll = Roll(
            tmp_path,
            _runtime(),
            status_after_update="UPDATE_FAILED",
            failure_reason_after_update="A lifecycle hook failed. Check your runtime's logs.",
        ).run("--expect-warmup-hook")

        assert roll.code == 5
        assert "failureReason: A lifecycle hook failed." in roll.log
        assert "build-logs-* streams of /aws/bedrock-agentcore/runtimes/rt-1-DEFAULT" in roll.log

    def test_the_next_image_is_sent_to_a_runtime_left_in_update_failed(self, tmp_path):
        """A failed update leaves the previous version serving, and the fix is
        the next update. Refusing would wedge every later backend deploy."""
        roll = Roll(
            tmp_path, _runtime(status="UPDATE_FAILED", failureReason="A lifecycle hook failed.")
        ).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert len(roll.updates) == 1
        assert "The previous update failed: A lifecycle hook failed." in roll.log
        assert roll.state["runtime"]["status"] == "READY"
        assert roll.updates[0]["hooksConfiguration"] == WARMUP_HOOKS

    @pytest.mark.parametrize("status", ["CREATE_FAILED", "DELETING"])
    def test_other_terminal_states_still_stop_it(self, tmp_path, status):
        roll = Roll(tmp_path, _runtime(status=status)).run()

        assert roll.code == 4
        assert roll.updates == []


class TestAnImageThatDoesNotServeTheHook:
    """A runtime with hooks calls them on the new image. One built before the
    route existed answers 404 and fails the update, so it is refused first."""

    def test_is_refused_before_anything_is_sent(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), image_labels={"new": {}}).run("--expect-warmup-hook")

        assert roll.code == 10
        assert roll.updates == []
        assert "does not declare" in roll.log
        assert "set CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED to false and run platform.yml" in roll.log

    def test_is_refused_whoever_configured_the_hook(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), image_labels={"new": {}}).run()

        assert roll.code == 10
        assert roll.updates == []

    def test_an_image_that_declares_the_hook_is_rolled(self, tmp_path):
        roll = Roll(tmp_path, _runtime()).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert "Target image declares org.agentcore-public-stack.runtime-warmup-hook=warmup-v1" in roll.log

    def test_an_image_that_cannot_be_inspected_is_rolled_with_a_warning(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), image_labels={"new": "unreadable"}).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert len(roll.updates) == 1
        assert "could not read org.agentcore-public-stack.runtime-warmup-hook" in roll.log

    def test_a_runtime_without_hooks_takes_any_image(self, tmp_path):
        roll = Roll(tmp_path, _runtime(hooksConfiguration=None), image_labels={"new": {}}).run()

        assert roll.code == 0, roll.log
        assert len(roll.updates) == 1


class TestPlatformPreflight:
    """scripts/platform/check-runtime-warmup-hook-image.sh: a CDK deploy that
    configures the hook is refused while the live image does not serve it."""

    CHECK = REPO_ROOT / "scripts" / "platform" / "check-runtime-warmup-hook-image.sh"
    LIVE = "123456789012.dkr.ecr.us-west-2.amazonaws.com/test-inference-api:new"

    def _run(self, tmp_path: Path, *, hooked: bool, image_uri: str = LIVE, labels: Any = None, **env: str):
        template = {
            "Resources": {
                "Runtime": {
                    "Type": "AWS::BedrockAgentCore::Runtime",
                    "Properties": (
                        {"HooksConfiguration": {"Build": {"Warmup": {"Action": {"HttpPost": {"Path": "/warmup"}}}}}}
                        if hooked
                        else {}
                    ),
                }
            }
        }
        path = tmp_path / "template.json"
        path.write_text(json.dumps(template))
        roll = Roll(tmp_path, _runtime(), image_labels={"new": HOOK_LABEL if labels is None else labels})
        return subprocess.run(
            ["bash", str(self.CHECK), str(path)],
            env={**roll.env, "CDK_PROJECT_PREFIX": "test", "CDK_AWS_REGION": "us-west-2", "RUNTIME_IMAGE_URI": image_uri, **env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_a_template_without_hooks_is_never_checked(self, tmp_path):
        result = self._run(tmp_path, hooked=False, labels={})
        assert result.returncode == 0, result.stderr
        assert "no lifecycle hooks" in result.stdout

    def test_a_live_image_that_serves_the_hook_passes(self, tmp_path):
        result = self._run(tmp_path, hooked=True)
        assert result.returncode == 0, result.stderr
        assert "safe to configure the hook" in result.stdout

    def test_a_live_image_without_the_route_stops_the_deploy(self, tmp_path):
        result = self._run(tmp_path, hooked=True, labels={})
        assert result.returncode == 1
        assert "does not serve it" in result.stderr
        assert "run the Backend Deploy workflow (backend.yml) first" in result.stderr

    def test_the_bootstrap_image_stops_the_deploy(self, tmp_path):
        """A first deploy starts from the CDK bootstrap image, which has no route."""
        result = self._run(
            tmp_path,
            hooked=True,
            image_uri="123456789012.dkr.ecr.us-west-2.amazonaws.com/cdk-hnb659fds-container-assets-123456789012-us-west-2:abc",
        )
        assert result.returncode == 1
        assert "the CDK bootstrap image, or a custom one" in result.stderr

    def test_an_unreadable_image_stops_the_deploy_and_names_the_override(self, tmp_path):
        result = self._run(tmp_path, hooked=True, labels="unreadable")
        assert result.returncode == 1
        assert "SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK=true" in result.stderr

    def test_the_override_skips_the_check(self, tmp_path):
        result = self._run(tmp_path, hooked=True, labels={}, SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK="true")
        assert result.returncode == 0, result.stderr

    def test_the_image_declares_the_label_the_checks_read(self):
        dockerfile = (REPO_ROOT / "backend" / "Dockerfile.inference-api").read_text()
        assert 'LABEL org.agentcore-public-stack.runtime-warmup-hook="warmup-v1"' in dockerfile

    def test_the_platform_deploy_runs_the_check_before_cdk_deploy(self):
        deploy = (REPO_ROOT / "scripts" / "platform" / "deploy.sh").read_text()
        assert deploy.index("check-runtime-warmup-hook-image.sh\" \\") < deploy.index("npx cdk deploy")

    def test_platform_yml_forwards_the_override(self):
        text = (REPO_ROOT / ".github" / "workflows" / "platform.yml").read_text()
        assert (
            "SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK: ${{ vars.SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK }}" in text
        )


class TestPlatformVersionStillSurvives:
    """Spec §3 B1, unchanged by the hook work."""

    def test_the_update_carries_the_platform_version(self, tmp_path):
        roll = Roll(tmp_path, _runtime()).run()

        assert roll.code == 0, roll.log
        assert roll.updates[0]["platformVersion"] == "V2"
        assert "Platform version unchanged: V2" in roll.log

    def test_a_cli_that_does_not_know_it_is_refused(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), cli_unknown_fields=["platformVersion"]).run()

        assert roll.code == 7
        assert roll.updates == []

    def test_a_platform_version_that_changed_fails_the_job(self, tmp_path):
        roll = Roll(tmp_path, _runtime(), service_overrides={"platformVersion": "V1"}).run()

        assert roll.code == 8

    def test_read_only_fields_are_not_sent(self, tmp_path):
        roll = Roll(tmp_path, _runtime()).run()

        assert not {"agentRuntimeArn", "agentRuntimeVersion", "status", "clientToken"} & set(roll.updates[0])


class TestNothingToDo:
    def test_a_runtime_already_on_the_image_is_left_alone(self, tmp_path):
        runtime = _runtime(agentRuntimeArtifact={"containerConfiguration": {"containerUri": NEW_URI}})
        roll = Roll(tmp_path, runtime).run("--expect-warmup-hook")

        assert roll.code == 0, roll.log
        assert roll.updates == []


class TestWrapperPassesTheExpectation:
    """deploy-runtime-image-one.sh turns the two deployment switches into
    `--expect-warmup-hook`, parsed the way infrastructure/lib/config.ts does."""

    def _args(self, tmp_path: Path, **env: str) -> List[str]:
        build = tmp_path / "scripts" / "build"
        common = tmp_path / "scripts" / "common"
        build.mkdir(parents=True)
        common.mkdir(parents=True)
        shutil.copy(WRAPPER, build / WRAPPER.name)
        (common / "load-env.sh").write_text(
            'log_info() { :; }\n'
            'export CDK_AWS_REGION=us-west-2 CDK_AWS_ACCOUNT=123456789012 CDK_PROJECT_PREFIX=test\n'
        )
        (build / SCRIPT.name).write_text('printf "%s\\n" "$@" > "$ARGS_OUT"\necho tag\n')
        out = tmp_path / "args.txt"
        base = {k: v for k, v in os.environ.items() if not k.startswith("CDK_")}
        result = subprocess.run(
            ["bash", str(build / WRAPPER.name), "inference-api"],
            env={**base, **env, "ARGS_OUT": str(out)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return out.read_text().split()

    @pytest.mark.parametrize("switch", ["true", "TRUE", " True "])
    def test_both_switches_on(self, tmp_path, switch):
        args = self._args(
            tmp_path,
            CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED=switch,
            CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION="V2",
        )
        assert "--expect-warmup-hook" in args

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED": "", "CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION": "V2"},
            {"CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED": "false", "CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION": "V2"},
            {"CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED": "1", "CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION": "V2"},
            # The hook is V2 only: CloudFormation configures none on V1.
            {"CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED": "true", "CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION": "V1"},
            {"CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED": "true"},
        ],
    )
    def test_anything_else_passes_nothing(self, tmp_path, env):
        args = self._args(tmp_path, **env)
        assert "--expect-warmup-hook" not in args
        assert args[:2] == ["--service", "inference-api"]


class TestWorkflowsForwardTheSwitches:
    """A switch platform.yml reads and backend.yml does not would configure
    the hook and then let an old CLI drop it."""

    @pytest.mark.parametrize("workflow", ["platform.yml", "backend.yml"])
    def test_both_workflows_read_the_same_variable(self, workflow):
        text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text()
        assert (
            "CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED: ${{ vars.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED }}" in text
        )

    def test_backend_also_reads_the_platform_version(self):
        text = (REPO_ROOT / ".github" / "workflows" / "backend.yml").read_text()
        assert (
            "CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION: ${{ vars.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION }}" in text
        )
