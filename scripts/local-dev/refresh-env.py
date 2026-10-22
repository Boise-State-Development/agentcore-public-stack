#!/usr/bin/env python3
"""Refresh backend/src/.env from the deployed app-api ECS task definition.

The task definition is the authoritative set of environment values for the App
API, so a stale local `.env` is best fixed by diffing against it rather than by
hand-editing whatever broke today. A pool replacement on 2026-06-23 left this
file three months behind and produced a bare Cognito `invalid_request`, a 502 on
`/system-prompts` and a silently unregistered `/artifacts` router.

Local overrides are preserved by name. Everything else is taken from the
deployment, and keys present locally but absent from the deployment are left
alone — they are local-only settings (AWS_PROFILE, feature bypasses, API keys)
and deleting them would break the very thing this script exists to fix.

Usage:
    python3 scripts/local-dev/refresh-env.py            # dry run
    python3 scripts/local-dev/refresh-env.py --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "backend/src/.env"

#: Values that are *supposed* to differ locally. Taking the deployment's copy of
#: any of these would point the local stack at the deployed environment and, for
#: the two BFF URLs, break the OAuth round trip outright.
KEEP_LOCAL = {
    "BFF_AUTH_CALLBACK_URL",       # localhost:8000; registered on the Cognito client
    "BFF_POST_LOGIN_REDIRECT_URL",  # localhost:4200
    "CORS_ORIGINS",                 # the local origin list
    "FRONTEND_URL",                 # localhost:4200
    "AGENTCORE_RUNTIME_WORKLOAD_NAME",  # local_dev_inference
    "INFERENCE_API_URL",            # the locally-run inference API on :8001
}

#: Local values for keys the deployment has but that must not be copied verbatim.
LOCAL_VALUES = {
    # Run the inference API locally rather than proxying to the deployed
    # AgentCore runtime, so agent changes are testable without a deploy.
    "INFERENCE_API_URL": "http://localhost:8001",
}


def parse_env(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value
    return values


def deployed_env(task: str, profile: str, region: str) -> dict:
    result = subprocess.run(
        [
            "aws", "ecs", "describe-task-definition",
            "--task-definition", task,
            "--profile", profile,
            "--region", region,
            "--query", "taskDefinition.containerDefinitions[0].environment",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"could not read task definition {task}:\n{result.stderr}")
    return {item["name"]: item["value"] for item in json.loads(result.stdout)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the file")
    parser.add_argument("--task", default="dev-boisestateai-v2-app-api-task")
    parser.add_argument("--profile", default="boisestate.ai")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    original = ENV_FILE.read_text()
    local = parse_env(original)
    remote = deployed_env(args.task, args.profile, args.region)

    updates, additions, kept = {}, {}, []
    for key, value in sorted(remote.items()):
        target = LOCAL_VALUES.get(key, value)
        if key in KEEP_LOCAL and key not in LOCAL_VALUES:
            if key in local:
                kept.append(key)
                continue
        if key not in local:
            additions[key] = target
        elif local[key] != target:
            updates[key] = (local[key], target)

    local_only = sorted(set(local) - set(remote))

    print(f"{len(updates)} updated · {len(additions)} added · "
          f"{len(kept)} local overrides kept · {len(local_only)} local-only untouched\n")
    if updates:
        print("── updated ──")
        for key, (was, now) in sorted(updates.items()):
            print(f"  {key}\n      - {was}\n      + {now}")
    if additions:
        print("\n── added ──")
        for key, value in sorted(additions.items()):
            print(f"  {key}={value}")
    if kept:
        print("\n── kept local (not overwritten) ──")
        for key in sorted(kept):
            print(f"  {key}={local[key]}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    lines = original.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key][1]}")
                continue
        out.append(line)

    if additions:
        out.append("")
        out.append(f"# Added from {args.task} on "
                   f"{datetime.now().strftime('%Y-%m-%d')} by refresh-env.py.")
        for key, value in sorted(additions.items()):
            out.append(f"{key}={value}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ENV_FILE.with_suffix(f".backup-{stamp}")
    shutil.copy(ENV_FILE, backup)
    ENV_FILE.write_text("\n".join(out) + "\n")
    print(f"\nWrote {ENV_FILE}\nBackup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
