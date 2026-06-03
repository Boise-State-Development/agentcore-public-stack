#!/usr/bin/env bash
#============================================================
# deploy-lambda-code-if-changed.sh — content-hash-aware Lambda
# zip code deploy.
#
# Computes a content hash of a Lambda's source directory, compares
# it to a published value in SSM, and only runs `aws lambda
# update-function-code` if the hash has changed.
#
# This is the zip-Lambda equivalent of build-and-push-if-changed.sh
# (which handles Docker-image Lambdas + ECS task images). The two
# script shapes are intentionally parallel — both:
#   1. Compute a deterministic content hash of the inputs.
#   2. Compare to whatever's already in AWS (ECR / SSM-tracked).
#   3. Skip the deploy if nothing changed.
#   4. Wait for AWS to reach a steady state if a deploy was needed.
#
# Why a hash + SSM tracker instead of just always uploading?
#   Because PlatformStack's bootstrap-zip pattern depends on the
#   Lambda's `Code` property never appearing to drift in CFN's eyes.
#   `update-function-code` modifies the actual Lambda but doesn't
#   touch the CFN model; CFN's stored `Code: { S3Bucket, S3Key }`
#   stays at the bootstrap value forever. As long as we only call
#   `update-function-code` when the source actually changed, we
#   minimise the gap between live state and CFN state to "the live
#   code is whatever the workflow last shipped, the CFN model is
#   the bootstrap." (Drift detection would surface this if anyone
#   ran it manually, but normal stack updates leave the Lambda
#   alone.)
#
# Liveness guard (why a source hash alone is NOT enough):
#   The source hash answers "did the code we ship change?" — but it
#   is decoupled from what is ACTUALLY live on the function. A CFN /
#   Platform deploy that replaces the Lambda (logical-id change) or
#   otherwise resets its `Code` property reverts it to the bootstrap
#   stub WITHOUT touching the source, so a source-hash-only check
#   would skip forever and strand the stub in production. That is
#   exactly what happened to the artifact-render Lambda after it was
#   hoisted into PlatformStack: the function was replaced (reset to
#   the 503 stub) while `render-code-hash` still held the previous
#   real-code hash, so every workflow run logged "unchanged — skip"
#   and the placeholder stayed live.
#   So we ALSO record the CodeSha256 of what we shipped, and compare
#   it against the function's LIVE CodeSha256 each run. If they
#   differ, the live code drifted from what we shipped (someone reset
#   the function out-of-band) and we re-deploy regardless of the
#   source hash. Both must match to skip.
#
# Usage:
#   deploy-lambda-code-if-changed.sh \
#     --service        artifact-render \
#     --source-dir     backend/src/lambdas/artifact_render \
#     --function-name-ssm  /ai-sbmt-api/artifacts/render-function-name \
#     --code-hash-ssm      /ai-sbmt-api/artifacts/render-code-hash \
#     --code-sha256-ssm    /ai-sbmt-api/artifacts/render-code-sha256
#
# Required env:
#   AWS_REGION          (e.g., us-west-2)
#============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPUTE_HASH="${SCRIPT_DIR}/compute-content-hash.sh"

SERVICE=""
SOURCE_DIR=""
FUNCTION_NAME_SSM=""
CODE_HASH_SSM=""
CODE_SHA256_SSM=""

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME --source-dir DIR \\
          --function-name-ssm PATH --code-hash-ssm PATH \\
          --code-sha256-ssm PATH
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)            SERVICE="$2"; shift 2 ;;
        --source-dir)         SOURCE_DIR="$2"; shift 2 ;;
        --function-name-ssm)  FUNCTION_NAME_SSM="$2"; shift 2 ;;
        --code-hash-ssm)      CODE_HASH_SSM="$2"; shift 2 ;;
        --code-sha256-ssm)    CODE_SHA256_SSM="$2"; shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"           ]] || { echo "missing --service" >&2; usage; }
[[ -n "$SOURCE_DIR"        ]] || { echo "missing --source-dir" >&2; usage; }
[[ -n "$FUNCTION_NAME_SSM" ]] || { echo "missing --function-name-ssm" >&2; usage; }
[[ -n "$CODE_HASH_SSM"     ]] || { echo "missing --code-hash-ssm" >&2; usage; }
[[ -n "$CODE_SHA256_SSM"   ]] || { echo "missing --code-sha256-ssm" >&2; usage; }
[[ -n "${AWS_REGION:-}"    ]] || { echo "AWS_REGION env var required" >&2; exit 2; }
[[ -d "$SOURCE_DIR"        ]] || { echo "source-dir not found: $SOURCE_DIR" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# 1. Compute content hash of the source directory.
# We reuse compute-content-hash.sh by giving it the source dir as
# both the dockerfile-equivalent (the handler entry point) and the
# source-dir. The script requires --dockerfile, so we pass the
# handler.py as the "manifest" (it's what CDK uses as the entry
# point, equivalent to a Dockerfile in the build sense).
HANDLER_FILE="${SOURCE_DIR}/handler.py"
[[ -f "$HANDLER_FILE" ]] || { echo "expected handler.py at $HANDLER_FILE" >&2; exit 2; }

log "Computing content hash of $SOURCE_DIR..."
HASH="$(bash "$COMPUTE_HASH" \
    --dockerfile "$HANDLER_FILE" \
    --source-dir "$SOURCE_DIR")"
log "Content hash: $HASH"

# 2. Resolve the function name from SSM. The Lambda's name is
# CDK-auto-generated to avoid orphan-collisions, so we can't hard-
# code it here. Resolved up-front (before the skip decision) because
# the liveness guard below needs to read the function's live
# CodeSha256. CDK runs before this step and publishes the name, so
# the parameter always exists by the time we get here.
FUNCTION_NAME="$(aws ssm get-parameter \
    --region "$AWS_REGION" \
    --name "$FUNCTION_NAME_SSM" \
    --query 'Parameter.Value' \
    --output text)"
log "Function name: $FUNCTION_NAME"

# 3. Gather the three signals the skip decision needs:
#    a. PUBLISHED_HASH — source hash we last shipped (SSM).
#    b. RECORDED_SHA   — CodeSha256 we recorded on that same deploy
#                        (SSM). Empty on a first deploy or before this
#                        liveness guard existed.
#    c. LIVE_SHA       — the function's CURRENT CodeSha256 (live).
# Missing SSM parameters (first deploy) count as "changed".
PUBLISHED_HASH=""
if PUBLISHED_HASH="$(aws ssm get-parameter \
        --region "$AWS_REGION" \
        --name "$CODE_HASH_SSM" \
        --query 'Parameter.Value' \
        --output text 2>/dev/null)"; then
    log "Published source hash: $PUBLISHED_HASH"
else
    log "No published source hash yet (first deploy)."
    PUBLISHED_HASH=""
fi

RECORDED_SHA=""
if RECORDED_SHA="$(aws ssm get-parameter \
        --region "$AWS_REGION" \
        --name "$CODE_SHA256_SSM" \
        --query 'Parameter.Value' \
        --output text 2>/dev/null)"; then
    log "Recorded CodeSha256: $RECORDED_SHA"
else
    log "No recorded CodeSha256 yet (first deploy / pre-liveness-guard)."
    RECORDED_SHA=""
fi

LIVE_SHA="$(aws lambda get-function-configuration \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --query 'CodeSha256' \
    --output text)"
log "Live CodeSha256: $LIVE_SHA"

# Skip ONLY when the source is unchanged AND the live code is exactly
# what we last shipped. The CodeSha256 comparison is the guard that
# defends against a CFN/Platform deploy silently reverting the
# function to the bootstrap stub: in that case LIVE_SHA != RECORDED_SHA
# and we re-deploy even though the source never changed.
if [[ "$HASH" == "$PUBLISHED_HASH" && -n "$RECORDED_SHA" && "$LIVE_SHA" == "$RECORDED_SHA" ]]; then
    log "Source unchanged and live code matches last deploy — skipping update-function-code."
    echo "$HASH"
    exit 0
fi

# Log WHY we're deploying (the three triggers, in priority order).
if [[ "$HASH" != "$PUBLISHED_HASH" ]]; then
    log "Source changed (was '$PUBLISHED_HASH', now '$HASH') — deploying."
elif [[ -z "$RECORDED_SHA" ]]; then
    log "No recorded CodeSha256 — deploying to establish the liveness baseline."
else
    log "LIVE DRIFT: live CodeSha256 ($LIVE_SHA) != last-deployed ($RECORDED_SHA). The function was reset out-of-band — almost certainly a CFN/Platform deploy reverting it to the bootstrap stub — so the real handler is NOT live. Re-shipping."
fi

# 4. Zip the source directory. We run from inside the dir so the
# zip entries are relative — Lambda extracts them at the runtime's
# working directory and `handler.handler` resolves correctly.
TMP_ZIP="$(mktemp -t "${SERVICE}-XXXXXX.zip")"
trap 'rm -f "$TMP_ZIP"' EXIT
# mktemp creates the file empty; zip would interpret an existing
# empty file as a malformed archive and exit 3. Remove it first
# so zip starts from a clean slate. The trap above still cleans
# up the new file zip writes.
rm -f "$TMP_ZIP"
log "Zipping source to $TMP_ZIP..."
(cd "$SOURCE_DIR" && zip -r -q -X "$TMP_ZIP" . -x '__pycache__/*' '*.pyc' '.DS_Store')

# 5. Wait for the function to be in a state where update-function-code
# is accepted. After CDK creates the function or any other update,
# AWS reports State=Active|LastUpdateStatus=Successful within seconds
# but the actual gate is LastUpdateStatus.
log "Waiting for function to be ready for update..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2 || {
        # `wait function-updated` returns non-zero only on InvalidState,
        # which means the function doesn't exist or is in a fundamentally
        # broken state. Surface the real status to help debugging.
        aws lambda get-function-configuration \
            --region "$AWS_REGION" \
            --function-name "$FUNCTION_NAME" \
            --query '{State:State,LastUpdateStatus:LastUpdateStatus,StateReason:StateReason}' >&2 || true
        exit 3
    }

# 6. Update.
log "Calling aws lambda update-function-code..."
aws lambda update-function-code \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://${TMP_ZIP}" \
    --no-cli-pager \
    --output text \
    --query 'FunctionArn' >/dev/null

# 7. Wait for the update to complete.
log "Waiting for update to settle..."
aws lambda wait function-updated \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" >&2

# 8. Read the settled CodeSha256 and publish BOTH trackers so the
# next run can short-circuit — and, crucially, so the liveness guard
# has a baseline to compare the live code against. Reading the
# settled value (rather than trusting update-function-code's echo)
# guarantees we record what AWS actually has live.
log "Reading settled CodeSha256..."
NEW_SHA="$(aws lambda get-function-configuration \
    --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" \
    --query 'CodeSha256' \
    --output text)"

log "Publishing source hash to $CODE_HASH_SSM and CodeSha256 to $CODE_SHA256_SSM..."
aws ssm put-parameter \
    --region "$AWS_REGION" \
    --name "$CODE_HASH_SSM" \
    --value "$HASH" \
    --type String \
    --overwrite \
    --no-cli-pager >/dev/null
aws ssm put-parameter \
    --region "$AWS_REGION" \
    --name "$CODE_SHA256_SSM" \
    --value "$NEW_SHA" \
    --type String \
    --overwrite \
    --no-cli-pager >/dev/null

log "Done. Source hash: $HASH, CodeSha256: $NEW_SHA"
echo "$HASH"
