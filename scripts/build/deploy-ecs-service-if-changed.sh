#!/usr/bin/env bash
#============================================================
# deploy-ecs-service-if-changed.sh — content-hash-aware ECS
# Fargate service image deploy.
#
# Mirror of deploy-image-lambda-if-changed.sh, but for an ECS
# Fargate service. Lambda has a single API call
# (update-function-code) that swaps the image in place. ECS's
# equivalent is a 3-step lifecycle:
#
#   1. Read the live task definition (the one CDK created on
#      first deploy with the bootstrap image, OR the most-recent
#      revision the workflow itself registered last time).
#   2. Mutate its containerDefinitions[].image to the new ECR URI.
#   3. register-task-definition (returns a new revision ARN).
#   4. update-service --task-definition <family>:<new-rev>.
#   5. Wait services-stable (all tasks healthy on the new rev).
#
# This script wraps that lifecycle with content-hash short-
# circuiting: if the live task def's image already points at the
# target URI, skip steps 2-5.
#
# Usage:
#   deploy-ecs-service-if-changed.sh \
#     --service           app-api \
#     --cluster-name-ssm  /ai-sbmt-api/app-api/cluster-name \
#     --service-name-ssm  /ai-sbmt-api/app-api/service-name \
#     --task-def-family-ssm /ai-sbmt-api/app-api/task-def-family \
#     --image-uri-ssm     /ai-sbmt-api/app-api/image-tag \
#     --ecr-repo-uri      327491786490.dkr.ecr.us-west-2.amazonaws.com/ai-sbmt-api-app-api
#
# Required env:
#   AWS_REGION            (e.g., us-west-2)
#============================================================
set -euo pipefail

SERVICE=""
CLUSTER_NAME_SSM=""
SERVICE_NAME_SSM=""
TASK_DEF_FAMILY_SSM=""
IMAGE_URI_SSM=""
ECR_REPO_URI=""

usage() {
    cat <<EOF >&2
Usage: $0 --service NAME \\
          --cluster-name-ssm PATH --service-name-ssm PATH \\
          --task-def-family-ssm PATH \\
          --image-uri-ssm PATH --ecr-repo-uri URI
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)              SERVICE="$2"; shift 2 ;;
        --cluster-name-ssm)     CLUSTER_NAME_SSM="$2"; shift 2 ;;
        --service-name-ssm)     SERVICE_NAME_SSM="$2"; shift 2 ;;
        --task-def-family-ssm)  TASK_DEF_FAMILY_SSM="$2"; shift 2 ;;
        --image-uri-ssm)        IMAGE_URI_SSM="$2"; shift 2 ;;
        --ecr-repo-uri)         ECR_REPO_URI="$2"; shift 2 ;;
        -h|--help)              usage ;;
        *)                      echo "Unknown arg: $1" >&2; usage ;;
    esac
done

[[ -n "$SERVICE"             ]] || { echo "missing --service" >&2; usage; }
[[ -n "$CLUSTER_NAME_SSM"    ]] || { echo "missing --cluster-name-ssm" >&2; usage; }
[[ -n "$SERVICE_NAME_SSM"    ]] || { echo "missing --service-name-ssm" >&2; usage; }
[[ -n "$TASK_DEF_FAMILY_SSM" ]] || { echo "missing --task-def-family-ssm" >&2; usage; }
[[ -n "$IMAGE_URI_SSM"       ]] || { echo "missing --image-uri-ssm" >&2; usage; }
[[ -n "$ECR_REPO_URI"        ]] || { echo "missing --ecr-repo-uri" >&2; usage; }
[[ -n "${AWS_REGION:-}"      ]] || { echo "AWS_REGION env var required" >&2; exit 2; }

log() { echo "[$SERVICE] $*" >&2; }

# 1. Resolve every SSM-backed input.
ssm_get() {
    aws ssm get-parameter --region "$AWS_REGION" --name "$1" \
        --query 'Parameter.Value' --output text
}
# The image-uri SSM holds the FULL ECR URI (registry/repo:tag) per
# the platform-as-bootstrap design — the CDK construct reads the
# same value directly into the ECS TaskDefinition Image at deploy
# time, and CFN's regex validation rejects bare tags. build-one.sh
# is the canonical writer.
NEW_IMAGE_URI="$(ssm_get "$IMAGE_URI_SSM")"

# Sanity-check that the SSM value is a real ECR URI, not a stale
# tag-only legacy value left over from a pre-platform-as-bootstrap
# deploy. The platform deploy's seed script repairs these, but we'd
# rather fail loud here than register a bogus task definition.
ECR_URI_REGEX='^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/([a-z0-9]+([._-][a-z0-9]+)*/)*[a-z0-9]+([._-][a-z0-9]+)*[:@][^[:space:]]+$'
if [[ ! "$NEW_IMAGE_URI" =~ $ECR_URI_REGEX ]]; then
    log "SSM ${IMAGE_URI_SSM} = '${NEW_IMAGE_URI}' is not a valid ECR URI."
    log "build-one.sh should have written REGISTRY/REPO:TAG. Re-run the build job, or run the platform deploy to re-seed the parameter."
    exit 6
fi
if [[ "$NEW_IMAGE_URI" != "${ECR_REPO_URI}:"* && "$NEW_IMAGE_URI" != "${ECR_REPO_URI}@"* ]]; then
    log "WARNING: SSM URI '${NEW_IMAGE_URI}' does not reference --ecr-repo-uri '${ECR_REPO_URI}'. Proceeding anyway."
fi

# Derive the tag for logging / GITHUB_OUTPUT echoing.
if [[ "$NEW_IMAGE_URI" == *@* ]]; then
    IMAGE_TAG="${NEW_IMAGE_URI##*@}"
else
    IMAGE_TAG="${NEW_IMAGE_URI##*:}"
fi
CLUSTER_NAME="$(ssm_get "$CLUSTER_NAME_SSM")"
SERVICE_NAME="$(ssm_get "$SERVICE_NAME_SSM")"
TASK_DEF_FAMILY="$(ssm_get "$TASK_DEF_FAMILY_SSM")"

log "Cluster:        $CLUSTER_NAME"
log "Service:        $SERVICE_NAME"
log "Task-def family: $TASK_DEF_FAMILY"
log "Target image:   $NEW_IMAGE_URI"

# 2. Read the latest task definition. ECS will return the current
# active revision when we ask by family name.
LIVE_TASK_DEF_JSON="$(aws ecs describe-task-definition \
    --region "$AWS_REGION" \
    --task-definition "$TASK_DEF_FAMILY" \
    --output json)"

CURRENT_IMAGE="$(printf '%s' "$LIVE_TASK_DEF_JSON" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin)["taskDefinition"];print(d["containerDefinitions"][0]["image"])')"
log "Current image:  $CURRENT_IMAGE"

if [[ "$CURRENT_IMAGE" == "$NEW_IMAGE_URI" ]]; then
    log "Task def already on $NEW_IMAGE_URI — skipping register/update."
    echo "$IMAGE_TAG"
    exit 0
fi

# 3. Build the new task definition by mutating containerDefinitions[].image.
# describe-task-definition returns extra fields that register-task-definition
# rejects (taskDefinitionArn, status, revision, etc.). The JSON pipeline
# below strips those and returns just the registerable shape.
NEW_TASK_DEF_JSON="$(printf '%s' "$LIVE_TASK_DEF_JSON" | python3 -c '
import json
import sys

td = json.load(sys.stdin)["taskDefinition"]

# Update the first container image. Single-container task defs only
# (assert that -- the project has just one app-api container per task).
# Precompute the count so the f-string does not need quote escapes
# inside its expression — backslash escapes are not permitted inside
# f-string `{...}` expressions, which broke an earlier inline form.
container_count = len(td["containerDefinitions"])
assert container_count == 1, f"expected 1 container, got {container_count}"
td["containerDefinitions"][0]["image"] = sys.argv[1]

# Drop fields that register-task-definition does not accept.
for k in (
    "taskDefinitionArn",
    "status",
    "revision",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
):
    td.pop(k, None)

print(json.dumps(td))
' "$NEW_IMAGE_URI")"

# 4. Register the new revision.
log "Registering new task definition revision..."
NEW_TD_ARN="$(aws ecs register-task-definition \
    --region "$AWS_REGION" \
    --cli-input-json "$NEW_TASK_DEF_JSON" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text)"
log "Registered: $NEW_TD_ARN"

# 5. Roll the service over.
log "Calling aws ecs update-service..."
aws ecs update-service \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER_NAME" \
    --service "$SERVICE_NAME" \
    --task-definition "$NEW_TD_ARN" \
    --no-cli-pager \
    --output text \
    --query 'service.serviceArn' >/dev/null

# 6. Wait for the rolling deployment to settle. The default is 40
# tries × 15 s = 10 min — plenty for a single-task service.
log "Waiting for services-stable..."
aws ecs wait services-stable \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER_NAME" \
    --services "$SERVICE_NAME" >&2 || {
        log "services-stable timed out; describe-services for diagnostics:"
        aws ecs describe-services \
            --region "$AWS_REGION" \
            --cluster "$CLUSTER_NAME" \
            --services "$SERVICE_NAME" \
            --query 'services[0].{Status:status,Deployments:deployments[*].{Status:status,Desired:desiredCount,Running:runningCount,Pending:pendingCount}}' >&2 || true
        exit 4
    }

log "Done. Service running on $NEW_IMAGE_URI."
echo "$IMAGE_TAG"
