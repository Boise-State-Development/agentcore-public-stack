#!/usr/bin/env bash
#============================================================
# check-runtime-warmup-hook-image.sh — refuse a PlatformStack deploy that
# configures the Runtime V2 build warmup hook while the LIVE inference-api
# image does not serve it.
#
# Why this exists
# ---------------
# With `hooksConfiguration.build.warmup` set, AgentCore calls POST /warmup on
# the container while it prepares each Runtime version, and a call that does
# not answer 2xx fails the create or update (docs/specs/agentcore-runtime-v2.md
# §5b). CFN re-registers the Runtime with whatever image is live (it reads
# /<prefix>/inference-api/image-tag at deploy time). An image built before the
# route existed answers 404, and so does the CDK bootstrap image a first
# deploy starts from: the Runtime update fails and CloudFormation rolls back.
#
# An image that serves the route carries a label,
# org.agentcore-public-stack.runtime-warmup-hook (backend/Dockerfile.inference-api).
# This script reads it off the live image and fails before `cdk deploy` when
# it is missing, with what to do. Nothing has been changed at that point.
#
# Passes (exit 0) when:
#   - the synthesized Runtime carries no HooksConfiguration (the switch is
#     off, or the platform version is V1);
#   - SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK=true (a custom image you have
#     checked serves POST /warmup);
#   - the live image declares the label.
#
# Usage: check-runtime-warmup-hook-image.sh <template.json>
#   Env: CDK_PROJECT_PREFIX, CDK_AWS_REGION (as loaded by load-env.sh).
#   Test hook: RUNTIME_IMAGE_URI overrides the SSM lookup.
#============================================================
set -euo pipefail

TEMPLATE="${1:?usage: check-runtime-warmup-hook-image.sh <template.json>}"
PREFIX="${CDK_PROJECT_PREFIX:?CDK_PROJECT_PREFIX is required}"
REGION="${CDK_AWS_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL_KEY="org.agentcore-public-stack.runtime-warmup-hook"
REQUIRED="warmup-v1"

say()  { echo "[runtime-warmup-hook] $*"; }
fail() { echo "::error title=Live inference-api image does not serve the warmup hook::$*" >&2; say "ERROR: $*" >&2; exit 1; }
REMEDY="Either set CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED to false for this deploy, or run the Backend Deploy \
workflow (backend.yml) first so an image that serves the hook is live, then re-run this Platform Stack deploy. \
Nothing has been changed. See docs/specs/agentcore-runtime-v2.md §5b."

HOOKED="$(python3 - "$TEMPLATE" <<'PY'
import json, sys
resources = json.load(open(sys.argv[1])).get("Resources", {})
runtimes = [r for r in resources.values() if r.get("Type") == "AWS::BedrockAgentCore::Runtime"]
print("yes" if any((r.get("Properties") or {}).get("HooksConfiguration") for r in runtimes) else "no")
PY
)"
if [ "$HOOKED" != "yes" ]; then
  say "The Runtime carries no lifecycle hooks; nothing to check."
  exit 0
fi
if [ "${SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK:-}" = "true" ]; then
  say "SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK=true; not checking the live image."
  exit 0
fi

IMAGE_URI="${RUNTIME_IMAGE_URI:-}"
if [ -z "$IMAGE_URI" ]; then
  IMAGE_URI="$(aws ssm get-parameter --region "$REGION" --name "/${PREFIX}/inference-api/image-tag" \
    --query Parameter.Value --output text 2>/dev/null || true)"
fi
if [ -z "$IMAGE_URI" ] || [ "$IMAGE_URI" = "None" ]; then
  fail "This deploy configures the Runtime's build warmup hook, but no inference-api image is recorded yet \
(a first deploy starts from the CDK bootstrap image, which does not serve POST /warmup). ${REMEDY}"
fi

REPO_PATH="${IMAGE_URI#*/}"
REPO="${REPO_PATH%@*}"; REPO="${REPO%:*}"
if [ "$REPO" != "${PREFIX}-inference-api" ]; then
  fail "This deploy configures the Runtime's build warmup hook, but the live image is in '${REPO}', not \
'${PREFIX}-inference-api' (the CDK bootstrap image, or a custom one). If it is a custom image that serves \
POST /warmup, set the GitHub variable SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK=true. Otherwise: ${REMEDY}"
fi

VALUE="$(bash "${SCRIPT_DIR}/../common/ecr-image-label.sh" "$IMAGE_URI" "$LABEL_KEY")" || fail "Could not read the \
live image (${IMAGE_URI##*/}). The deploy credentials need ecr:BatchGetImage and ecr:GetDownloadUrlForLayer on \
${REPO}. If you have confirmed that the live image serves POST /warmup, set the GitHub variable \
SKIP_RUNTIME_WARMUP_HOOK_IMAGE_CHECK=true for one run."

if [ "$VALUE" = "$REQUIRED" ]; then
  say "Live image ${IMAGE_URI##*/} declares ${LABEL_KEY}=${VALUE}; safe to configure the hook."
  exit 0
fi

fail "This deploy configures the Runtime's build warmup hook, but the live inference-api image (${IMAGE_URI##*/}) \
does not serve it (label ${LABEL_KEY} is '${VALUE:-absent}', need '${REQUIRED}'). AgentCore would call POST /warmup, \
get a 404, and fail the Runtime update. ${REMEDY}"
