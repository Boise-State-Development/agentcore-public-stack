#!/usr/bin/env bash
#============================================================
# seed-image-tags.sh — pre-deploy seed for compute-resource
# image-tag SSM parameters.
#
# Why this exists
# ---------------
# PlatformStack's app-api task definition (Image) and AgentCore
# Runtime (containerUri) read their container image at CFN deploy
# time from SSM parameters:
#   /<prefix>/app-api/image-tag
#   /<prefix>/inference-api/image-tag
# rather than baking a value into the synthesized template. This
# means subsequent CFN-driven re-registrations preserve the live
# image instead of reverting to a CDK-bundled bootstrap stub.
#
# But CFN's `AWS::SSM::Parameter::Value<String>` template parameter
# requires the SSM path to exist BEFORE the stack operation begins.
# On a fresh account (first cdk deploy), the parameter doesn't
# exist yet → CFN errors with "Unable to fetch parameters from
# parameter store".
#
# This script runs once per environment, before the first cdk
# deploy, to seed each parameter with the bootstrap container's
# cdk-assets ECR URI. Subsequent runs are no-ops because the
# parameter already exists (the build pipeline overwrites it on
# every real-image push).
#
# Pre-requisite: cdk synth has run (cdk.out/ exists), and
# cdk-assets publish has pushed the bootstrap images to the
# cdk-assets ECR repo. Both are taken care of by
# scripts/platform/deploy.sh, which calls this script between
# synth+publish and deploy.
#============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

source "${PROJECT_ROOT}/scripts/common/load-env.sh"

CDK_OUT="${PROJECT_ROOT}/infrastructure/cdk.out"
TEMPLATE="${CDK_OUT}/${CDK_PROJECT_PREFIX}-PlatformStack.template.json"

if [[ ! -f "$TEMPLATE" ]]; then
    log_error "Synthesized template not found: ${TEMPLATE}"
    log_error "Run scripts/platform/synth.sh (or cdk synth) first."
    exit 1
fi

# CDK's default bootstrap qualifier. Matches the cdk-assets repo
# naming convention: cdk-<qualifier>-container-assets-<acct>-<region>.
QUALIFIER="hnb659fds"
ASSETS_REPO="cdk-${QUALIFIER}-container-assets-${CDK_AWS_ACCOUNT}-${CDK_AWS_REGION}"
REGISTRY="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"

# Read the bootstrap asset hash from a CfnOutput. The compute
# constructs emit AppApiBootstrapImageHash and
# InferenceApiBootstrapImageHash exactly so this script can find
# them without parsing Fn::Sub expressions. CDK prefixes the
# logical ID with the construct path and suffixes with a hash
# (e.g., AppApiAppApiBootstrapImageHashCF20B848), so we look up by
# substring rather than exact name.
read_asset_hash() {
    local marker="$1"
    local val
    val=$(jq -r --arg m "$marker" '
        .Outputs
        | to_entries
        | map(select(.key | contains($m)))
        | (if length == 0 then empty
           elif length == 1 then .[0].value.Value
           else error("multiple outputs match marker; fix lookup")
           end)
    ' "$TEMPLATE")
    if [[ -z "$val" || "$val" == "null" ]]; then
        log_error "CfnOutput matching '${marker}' not found in ${TEMPLATE}."
        log_error "Check that the compute construct still emits the bootstrap-image-hash output."
        exit 1
    fi
    echo "$val"
}

seed_one() {
    local svc="$1"
    local output_marker="$2"
    local ssm_path="/${CDK_PROJECT_PREFIX}/${svc}/image-tag"

    if aws ssm get-parameter \
            --name "$ssm_path" \
            --region "$CDK_AWS_REGION" \
            >/dev/null 2>&1; then
        log_info "  ${svc}: SSM ${ssm_path} already exists — skipping seed (build pipeline owns it)"
        return 0
    fi

    local hash uri
    hash="$(read_asset_hash "$output_marker")"
    uri="${REGISTRY}/${ASSETS_REPO}:${hash}"

    log_info "  ${svc}: seeding SSM ${ssm_path} = ${uri}"
    aws ssm put-parameter \
        --name "$ssm_path" \
        --value "$uri" \
        --type String \
        --region "$CDK_AWS_REGION" \
        --description "Container image URI for ${svc}. Seeded on first deploy with bootstrap asset URI; overwritten by build pipeline on every real-image push." \
        >/dev/null
}

main() {
    log_info "Seeding compute-resource image-tag SSM params (first-deploy only)..."
    seed_one app-api       AppApiBootstrapImageHash
    seed_one inference-api InferenceApiBootstrapImageHash
    log_info "Image-tag seed complete."
}

main "$@"
