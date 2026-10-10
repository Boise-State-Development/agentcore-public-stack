#!/usr/bin/env bash
#============================================================
# check-runtime-env-contract.sh — refuse a PlatformStack deploy that would
# strip the Runtime's resource-name variables while the LIVE inference-api
# image cannot derive them.
#
# Why this exists
# ---------------
# Since the AgentCore Runtime V2 environment refactor
# (docs/specs/agentcore-runtime-v2.md §7), CDK no longer sends the Runtime the
# ~26 `{prefix}-{suffix}` table, bucket and workload names: the inference-api
# derives them from PROJECT_PREFIX at startup. An image built BEFORE that
# change cannot, and CFN re-registers the Runtime with whatever image is live
# (it reads /<prefix>/inference-api/image-tag at deploy time). So a deploy of
# this template over an old image produces a Runtime that starts with no table
# names: every turn fails until backend.yml rolls a newer image.
#
# That happens whenever the platform deploy runs before the backend deploy on
# an upgrade that crosses the change. platform.yml and backend.yml fire on the
# same push and race for one concurrency slot (backend usually wins), and a
# deployer who skips a release takes both halves at once.
#
# The newer image carries a label, org.agentcore-public-stack.runtime-env-contract
# (set in backend/Dockerfile.inference-api). This script reads it off the live
# image and fails before `cdk deploy` when it is missing, with what to do.
#
# Skips (exit 0) when:
#   - the synthesized Runtime still receives the names (an override, a fork);
#   - the image-tag parameter is absent, or points outside the
#     {prefix}-inference-api repository (first deploy: the CDK bootstrap image);
#   - SKIP_RUNTIME_ENV_CONTRACT_CHECK=true (a custom image you have checked).
#
# Usage: check-runtime-env-contract.sh <template.json>
#   Env: CDK_PROJECT_PREFIX, CDK_AWS_REGION (as loaded by load-env.sh).
#   Test hook: RUNTIME_IMAGE_URI overrides the SSM lookup.
#============================================================
set -euo pipefail

TEMPLATE="${1:?usage: check-runtime-env-contract.sh <template.json>}"
PREFIX="${CDK_PROJECT_PREFIX:?CDK_PROJECT_PREFIX is required}"
REGION="${CDK_AWS_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
REQUIRED_CONTRACT="derived-names-v1"
LABEL_KEY="org.agentcore-public-stack.runtime-env-contract"
# Any manifest variable will do as the marker; this one is read on every turn.
MARKER_VARIABLE="DYNAMODB_SESSIONS_METADATA_TABLE_NAME"

say()  { echo "[runtime-env-contract] $*"; }
fail() { echo "::error title=Runtime image cannot derive its resource names::$*" >&2; say "ERROR: $*" >&2; exit 1; }

if [ "${SKIP_RUNTIME_ENV_CONTRACT_CHECK:-}" = "true" ]; then
  say "SKIP_RUNTIME_ENV_CONTRACT_CHECK=true; not checking the live image."
  exit 0
fi

# 1. Does this template strip the names at all?
STRIPS="$(python3 - "$TEMPLATE" "$MARKER_VARIABLE" <<'PY'
import json, sys
template, marker = sys.argv[1], sys.argv[2]
resources = json.load(open(template)).get("Resources", {})
runtimes = [r for r in resources.values() if r.get("Type") == "AWS::BedrockAgentCore::Runtime"]
if not runtimes:
    print("none"); sys.exit(0)
env = runtimes[0].get("Properties", {}).get("EnvironmentVariables", {}) or {}
print("no" if marker in env else "yes")
PY
)"
case "$STRIPS" in
  none) say "No AgentCore Runtime in the template; nothing to check."; exit 0 ;;
  no)   say "The Runtime is still sent ${MARKER_VARIABLE}; any image works."; exit 0 ;;
esac

# 2. Which image is live?
IMAGE_URI="${RUNTIME_IMAGE_URI:-}"
if [ -z "$IMAGE_URI" ]; then
  IMAGE_URI="$(aws ssm get-parameter --region "$REGION" --name "/${PREFIX}/inference-api/image-tag" \
    --query Parameter.Value --output text 2>/dev/null || true)"
fi
if [ -z "$IMAGE_URI" ] || [ "$IMAGE_URI" = "None" ]; then
  say "No live inference-api image recorded yet (first deploy); nothing to check."
  exit 0
fi

REPO_PATH="${IMAGE_URI#*/}"          # <repo>:<tag> or <repo>@sha256:...
if [[ "$REPO_PATH" == *@* ]]; then
  REPO="${REPO_PATH%@*}"; IMAGE_ID="imageDigest=${REPO_PATH#*@}"
else
  REPO="${REPO_PATH%:*}"; IMAGE_ID="imageTag=${REPO_PATH##*:}"
fi
if [ "$REPO" != "${PREFIX}-inference-api" ]; then
  say "Live image is in '${REPO}', not '${PREFIX}-inference-api' (the CDK bootstrap image, or a custom one); not checking."
  say "A custom image must derive the names itself: see backend/src/apis/shared/config/derived_environment.json."
  exit 0
fi

# 3. Read the label off the image config (resolving a multi-arch index to arm64).
ACCEPT="application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json application/vnd.oci.image.manifest.v1+json application/vnd.docker.distribution.manifest.v2+json"
get_manifest() {
  # shellcheck disable=SC2086
  aws ecr batch-get-image --region "$REGION" --repository-name "$REPO" --image-ids "$1" \
    --accepted-media-types $ACCEPT --query 'images[0].imageManifest' --output text
}
MANIFEST="$(get_manifest "$IMAGE_ID")" || fail "Could not read the live image manifest (${IMAGE_URI}). \
The deploy credentials need ecr:BatchGetImage and ecr:GetDownloadUrlForLayer on ${REPO}. If you have confirmed \
that backend.yml has deployed an image from this release, set the GitHub variable SKIP_RUNTIME_ENV_CONTRACT_CHECK=true \
for one run."
CHILD="$(python3 -c '
import json, sys
m = json.loads(sys.stdin.read())
if "manifests" in m:
    arm = [d for d in m["manifests"] if d.get("platform", {}).get("architecture") == "arm64"]
    print((arm or m["manifests"])[0]["digest"])
' <<<"$MANIFEST")"
if [ -n "$CHILD" ]; then
  MANIFEST="$(get_manifest "imageDigest=${CHILD}")" || fail "Could not read the arm64 manifest of ${IMAGE_URI}."
fi
CONFIG_DIGEST="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["config"]["digest"])' <<<"$MANIFEST")"
URL="$(aws ecr get-download-url-for-layer --region "$REGION" --repository-name "$REPO" \
  --layer-digest "$CONFIG_DIGEST" --query downloadUrl --output text)" || fail "Could not fetch the image config of ${IMAGE_URI}."
CONTRACT="$(curl -fsSL "$URL" | python3 -c "
import json, sys
labels = (json.load(sys.stdin).get('config') or {}).get('Labels') or {}
print(labels.get('${LABEL_KEY}', ''))
")"

if [ "$CONTRACT" = "$REQUIRED_CONTRACT" ]; then
  say "Live image ${IMAGE_URI##*/} declares ${LABEL_KEY}=${CONTRACT}; safe to stop sending the names."
  exit 0
fi

fail "This deploy stops sending the Runtime its table and bucket names, but the live inference-api image \
(${IMAGE_URI##*/}) predates the change and cannot derive them (label ${LABEL_KEY} is '${CONTRACT:-absent}', \
need '${REQUIRED_CONTRACT}'). Deploying now would start the Runtime without them and fail every turn. \
Run the Backend Deploy workflow (backend.yml) first so a newer image is live, then re-run this Platform Stack deploy. \
Nothing has been changed. See docs/specs/agentcore-runtime-v2.md §8 and the docs-site page Deployment > AgentCore Runtime V2."
