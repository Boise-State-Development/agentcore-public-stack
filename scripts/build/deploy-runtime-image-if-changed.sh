#!/usr/bin/env bash
#============================================================
# deploy-runtime-image-if-changed.sh — content-hash-aware
# AgentCore Runtime image code deploy.
#
# Mirror of deploy-image-lambda-if-changed.sh, but for AgentCore
# Runtime instead of a Lambda function. Calls
# `aws bedrock-agentcore-control update-agent-runtime` with a new
# container URI and waits for the runtime to reach READY again.
#
# Usage:
#   deploy-runtime-image-if-changed.sh \
#     --service           inference-api \
#     --runtime-id-ssm    /ai-sbmt-api/inference-api/runtime-id \
#     --image-uri-ssm     /ai-sbmt-api/inference-api/image-tag \
#     --ecr-repo-uri      327491786490.dkr.ecr.us-west-2.amazonaws.com/ai-sbmt-api-inference-api
#
# Required env:
#   AWS_REGION            (e.g., us-west-2)
#
# Pre-requisites:
#   - The Runtime exists (PlatformStack deploy has run).
#   - build-and-push-if-changed.sh has already pushed a new image
#     to ECR and updated `--image-uri-ssm` with the FULL ECR URI
#     (registry/repo:tag). The platform-as-bootstrap design treats
#     this SSM parameter as a URI, not a tag — see scripts/build/
#     build-one.sh and scripts/stack-bootstrap/seed-image-tags.sh.
#
# AgentCore Runtime status semantics:
#   CREATING | UPDATING — transitional, can't accept update calls
#   READY              — accepts update calls
#   CREATE_FAILED | UPDATE_FAILED | DELETING — terminal/error
# This script waits up to ~20 minutes for READY before issuing the
# update, and then waits up to ~10 more for the update to settle.
#
# Fields CloudFormation owns (platformVersion, capacityProvider-
# Configuration, ...) must survive the image roll unchanged. The
# update payload therefore carries every field the CLI's own update
# skeleton accepts, the script refuses to run on a CLI too old to
# know platformVersion, and it fails the job if platformVersion
# differs after the update. See docs/specs/agentcore-runtime-v2.md
# §3 B1.
#============================================================
set -euo pipefail

SERVICE=""
RUNTIME_ID_SSM=""
IMAGE_URI_SSM=""
ECR_REPO_URI=""

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME --runtime-id-ssm PATH \\
          --image-uri-ssm PATH --ecr-repo-uri URI
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)         SERVICE="$2"; shift 2 ;;
        --runtime-id-ssm)  RUNTIME_ID_SSM="$2"; shift 2 ;;
        --image-uri-ssm)   IMAGE_URI_SSM="$2"; shift 2 ;;
        --ecr-repo-uri)    ECR_REPO_URI="$2"; shift 2 ;;
        -h|--help)         usage ;;
        *)                 echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"        ]] || { echo "missing --service" >&2; usage; }
[[ -n "$RUNTIME_ID_SSM" ]] || { echo "missing --runtime-id-ssm" >&2; usage; }
[[ -n "$IMAGE_URI_SSM"  ]] || { echo "missing --image-uri-ssm" >&2; usage; }
[[ -n "$ECR_REPO_URI"   ]] || { echo "missing --ecr-repo-uri" >&2; usage; }
[[ -n "${AWS_REGION:-}" ]] || { echo "AWS_REGION env var required" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# 1. Resolve the new container URI from SSM. Per the platform-as-
# bootstrap design, this SSM parameter holds the FULL ECR URI
# (registry/repo:tag), not just a tag — the CDK construct reads
# the same value directly into the Runtime's ContainerUri at deploy
# time, and CFN's regex validation rejects bare tags. build-one.sh
# is the canonical writer.
NEW_CONTAINER_URI="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$IMAGE_URI_SSM" \
    --query 'Parameter.Value' \
    --output text)"

# Sanity-check that the SSM value is a real ECR URI and not a stale
# tag-only legacy value (e.g. left over from a pre-platform-as-
# bootstrap deploy). The seed script in scripts/stack-bootstrap/
# repairs these on the next platform deploy, but we'd rather fail
# loud here than push an invalid update to AgentCore.
ECR_URI_REGEX='^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/([a-z0-9]+([._-][a-z0-9]+)*/)*[a-z0-9]+([._-][a-z0-9]+)*[:@][^[:space:]]+$'
if [[ ! "$NEW_CONTAINER_URI" =~ $ECR_URI_REGEX ]]; then
    log "SSM ${IMAGE_URI_SSM} = '${NEW_CONTAINER_URI}' is not a valid ECR URI."
    log "build-one.sh should have written REGISTRY/REPO:TAG. Re-run the build job, or run the platform deploy to re-seed the parameter."
    exit 6
fi

# Optional consistency check: the URI should reference --ecr-repo-uri.
# We don't enforce identity (build-one writes the same string we'd
# expect, but leave headroom for digest pins or alt repo names) —
# just warn loudly if it diverges.
if [[ "$NEW_CONTAINER_URI" != "${ECR_REPO_URI}:"* && "$NEW_CONTAINER_URI" != "${ECR_REPO_URI}@"* ]]; then
    log "WARNING: SSM URI '${NEW_CONTAINER_URI}' does not reference --ecr-repo-uri '${ECR_REPO_URI}'. Proceeding anyway."
fi

# Derive the tag for logging / GITHUB_OUTPUT echoing. Splits on the
# rightmost ':' or '@' so digest-pinned URIs work too.
if [[ "$NEW_CONTAINER_URI" == *@* ]]; then
    IMAGE_TAG="${NEW_CONTAINER_URI##*@}"
else
    IMAGE_TAG="${NEW_CONTAINER_URI##*:}"
fi
log "Target container URI: $NEW_CONTAINER_URI"

# 2. Resolve the runtime ID.
RUNTIME_ID="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$RUNTIME_ID_SSM" \
    --query 'Parameter.Value' \
    --output text)"
log "Runtime ID: $RUNTIME_ID"

# 3. Snapshot the runtime's current state. Skip the update if the
# container URI is already what we'd set it to, and bail with a
# clear error if the runtime is in a state where update isn't
# allowed.
STATE_JSON="$(aws bedrock-agentcore-control get-agent-runtime \
    --region "$AWS_REGION" \
    --agent-runtime-id "$RUNTIME_ID" \
    --output json 2>/dev/null)" || {
        echo "[$SERVICE] Failed to get-agent-runtime — runtime may not exist yet." >&2
        exit 3
    }

CURRENT_URI="$(printf '%s' "$STATE_JSON" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("agentRuntimeArtifact",{}).get("containerConfiguration",{}).get("containerUri","unknown"))')"
CURRENT_STATUS="$(printf '%s' "$STATE_JSON" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status","unknown"))')"
CURRENT_PLATFORM_VERSION="$(printf '%s' "$STATE_JSON" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("platformVersion",""))')"

log "Current container URI: $CURRENT_URI"
log "Current status: $CURRENT_STATUS"
log "Current platform version: ${CURRENT_PLATFORM_VERSION:-<not reported>}"

if [[ "$CURRENT_URI" == "$NEW_CONTAINER_URI" ]]; then
    log "Runtime already on $NEW_CONTAINER_URI — skipping update-agent-runtime."
    echo "$IMAGE_TAG"
    exit 0
fi

# 4. Confirm this CLI can carry platformVersion through the update.
# A CLI whose service model predates the field never parses it out
# of get-agent-runtime, so the full-replace update below would omit
# it and could silently return a V2 runtime to the service default.
# The update skeleton is generated locally from the CLI's own model,
# and its top-level keys are exactly the fields the update accepts.
UPDATE_SKELETON="$(aws bedrock-agentcore-control update-agent-runtime \
    --generate-cli-skeleton input 2>/dev/null)" || {
        log "This AWS CLI cannot generate an update-agent-runtime skeleton — is bedrock-agentcore-control available?"
        exit 7
    }
if ! printf '%s' "$UPDATE_SKELETON" \
    | python3 -c 'import json,sys;sys.exit(0 if "platformVersion" in json.load(sys.stdin) else 1)'; then
    log "This AWS CLI ($(aws --version 2>&1)) does not know update-agent-runtime's platformVersion."
    log "Updating with it would drop the runtime's platform version. Upgrade the CLI on this runner."
    exit 7
fi

# 5. Wait for READY. Poll up to ~20 minutes (120 attempts × 10 sec). A V1
# update reaches READY in seconds; a V2 update prepares and snapshots the
# environment first, which AWS documents as several minutes (the dev Runtime's
# first V2 update took 3 min 23 s, 2026-10-10). Ten minutes left little margin
# for a slow snapshot.
wait_for_ready() {
    local label="$1"
    local attempts=120
    while (( attempts > 0 )); do
        local s
        s="$(aws bedrock-agentcore-control get-agent-runtime \
            --region "$AWS_REGION" \
            --agent-runtime-id "$RUNTIME_ID" \
            --query 'status' --output text 2>/dev/null || echo unknown)"
        case "$s" in
            READY)
                log "$label: status=READY"
                return 0
                ;;
            CREATE_FAILED|UPDATE_FAILED|DELETING)
                log "$label: terminal status $s — aborting"
                return 1
                ;;
            CREATING|UPDATING)
                log "$label: status=$s — waiting 10 s"
                sleep 10
                ;;
            *)
                log "$label: status=$s — waiting 10 s"
                sleep 10
                ;;
        esac
        attempts=$((attempts - 1))
    done
    log "$label: timed out waiting for READY"
    return 1
}

if [[ "$CURRENT_STATUS" != "READY" ]]; then
    wait_for_ready "pre-update" || exit 4
fi

# 6. Update. update-agent-runtime is a FULL replacement API — it
# requires roleArn, networkConfiguration, and every other config
# field, not just the new artifact, and a field left out may reset
# to the service default. We rebuild the payload from
# get-agent-runtime, keeping every field the CLI's update skeleton
# accepts (the get response also carries read-only fields like
# agentRuntimeArn, agentRuntimeVersion, status, createdAt,
# lastUpdatedAt, and workloadIdentityDetails that the update API
# rejects). Deriving the set from the skeleton rather than a fixed
# list means a field AWS adds later is carried, not dropped. The
# container URI is the only field we mutate.
log "Building update payload from current runtime state..."
STATE_TMP="$(mktemp)"
SKELETON_TMP="$(mktemp)"
PAYLOAD_TMP="$(mktemp)"
trap 'rm -f "$STATE_TMP" "$SKELETON_TMP" "$PAYLOAD_TMP"' EXIT
printf '%s' "$STATE_JSON" > "$STATE_TMP"
printf '%s' "$UPDATE_SKELETON" > "$SKELETON_TMP"

# Pass inputs as tmpfile paths (not stdin) so the heredoc owns stdin
# for the python script body. python3 - SCRIPT_FROM_STDIN ARGV...
python3 - "$STATE_TMP" "$SKELETON_TMP" "$NEW_CONTAINER_URI" "$PAYLOAD_TMP" <<'PYEOF'
import json
import sys

state_path = sys.argv[1]
skeleton_path = sys.argv[2]
new_uri = sys.argv[3]
out_path = sys.argv[4]

with open(state_path) as fh:
    state = json.load(fh)
with open(skeleton_path) as fh:
    skeleton = json.load(fh)

# clientToken is an idempotency token, not runtime state; let the CLI
# generate a fresh one.
ALLOWED = set(skeleton) - {"clientToken"}

payload = {k: v for k, v in state.items() if k in ALLOWED}

# Swap the container URI. Force containerConfiguration shape so we
# overwrite any code-configuration shape that might have been there.
payload["agentRuntimeArtifact"] = {
    "containerConfiguration": {"containerUri": new_uri},
}

with open(out_path, "w") as fh:
    json.dump(payload, fh)
PYEOF

log "Calling aws bedrock-agentcore-control update-agent-runtime..."
aws bedrock-agentcore-control update-agent-runtime \
    --region "$AWS_REGION" \
    --cli-input-json "file://$PAYLOAD_TMP" \
    --no-cli-pager \
    --output text \
    --query 'agentRuntimeArn' >/dev/null

# 7. Wait for the update to settle.
wait_for_ready "post-update" || exit 5

# 8. The platform version is CloudFormation's to set, never this
# script's. If the update changed it, fail loudly: a runtime that
# flips between V1 and V2 on every backend deploy is otherwise
# invisible until the cost or latency numbers stop making sense.
if [[ -n "$CURRENT_PLATFORM_VERSION" ]]; then
    POST_PLATFORM_VERSION="$(aws bedrock-agentcore-control get-agent-runtime \
        --region "$AWS_REGION" \
        --agent-runtime-id "$RUNTIME_ID" \
        --output json \
        | python3 -c 'import json,sys;print(json.load(sys.stdin).get("platformVersion",""))')"
    if [[ "$POST_PLATFORM_VERSION" != "$CURRENT_PLATFORM_VERSION" ]]; then
        log "Platform version changed during the image update: '${CURRENT_PLATFORM_VERSION}' -> '${POST_PLATFORM_VERSION:-<not reported>}'."
        log "Re-run platform.yml to restore the version CloudFormation declares, and fix this script before the next backend deploy."
        exit 8
    fi
    log "Platform version unchanged: $POST_PLATFORM_VERSION"
else
    log "WARNING: get-agent-runtime reported no platformVersion before the update; nothing to verify."
fi

log "Done. Runtime now at $NEW_CONTAINER_URI"
echo "$IMAGE_TAG"
