#!/bin/bash
set -euo pipefail

# Script: Promote ECR Image
# Description: Copies a Docker image from a source ECR repository (SOURCE_PROJECT_PREFIX)
#              to a target ECR repository (CDK_PROJECT_PREFIX) within the same account.
#              This avoids rebuilding images when promoting across nightly environments.
#
# Usage: bash scripts/common/promote-ecr-image.sh <service-name>
#   e.g. bash scripts/common/promote-ecr-image.sh app-api
#
# Required environment variables:
#   CDK_AWS_ACCOUNT          - AWS account ID
#   CDK_AWS_REGION           - AWS region
#   CDK_PROJECT_PREFIX       - Target project prefix (e.g. nightly-mv)
#   SOURCE_PROJECT_PREFIX    - Source project prefix (e.g. agentcore)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log_info()    { echo "[INFO] $1"; }
log_error()   { echo "[ERROR] $1" >&2; }
log_success() { echo "[SUCCESS] $1"; }

main() {
    local service_name="${1:?Usage: promote-ecr-image.sh <service-name>}"

    # Validate required env vars
    for var in CDK_AWS_ACCOUNT CDK_AWS_REGION CDK_PROJECT_PREFIX SOURCE_PROJECT_PREFIX; do
        if [ -z "${!var:-}" ]; then
            log_error "${var} is not set"
            exit 1
        fi
    done

    local ecr_registry="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com"
    local src_repo="${SOURCE_PROJECT_PREFIX}-${service_name}"
    local dst_repo="${CDK_PROJECT_PREFIX}-${service_name}"

    log_info "Promoting image: ${src_repo} -> ${dst_repo}"

    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region "${CDK_AWS_REGION}" | \
        docker login --username AWS --password-stdin "${ecr_registry}"

    # Read the current image tag from the source SSM parameter
    local ssm_param="/${SOURCE_PROJECT_PREFIX}/${service_name}/image-tag"
    log_info "Reading image tag from SSM: ${ssm_param}"

    local image_tag
    image_tag=$(aws ssm get-parameter \
        --name "${ssm_param}" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}")

    if [ -z "${image_tag}" ]; then
        log_error "Could not read image tag from ${ssm_param}"
        exit 1
    fi

    log_info "Source image tag: ${image_tag}"

    local src_image="${ecr_registry}/${src_repo}:${image_tag}"
    local dst_image="${ecr_registry}/${dst_repo}:${image_tag}"

    # Ensure target ECR repository exists
    log_info "Ensuring target ECR repository exists: ${dst_repo}"
    if ! aws ecr describe-repositories --repository-names "${dst_repo}" --region "${CDK_AWS_REGION}" > /dev/null 2>&1; then
        log_info "Creating ECR repository: ${dst_repo}"
        aws ecr create-repository \
            --repository-name "${dst_repo}" \
            --region "${CDK_AWS_REGION}" \
            --image-scanning-configuration scanOnPush=true \
            --encryption-configuration encryptionType=AES256 \
            --tags Key=Project,Value="${CDK_PROJECT_PREFIX}" Key=ManagedBy,Value=GitHubActions
    fi

    # Pull from source, tag for target, push
    log_info "Pulling: ${src_image}"
    docker pull "${src_image}"

    log_info "Tagging: ${dst_image}"
    docker tag "${src_image}" "${dst_image}"

    log_info "Pushing: ${dst_image}"
    docker push "${dst_image}"

    # Store the tag in the target SSM parameter so CDK picks it up
    local dst_ssm_param="/${CDK_PROJECT_PREFIX}/${service_name}/image-tag"
    log_info "Writing image tag to SSM: ${dst_ssm_param} = ${image_tag}"
    aws ssm put-parameter \
        --name "${dst_ssm_param}" \
        --value "${image_tag}" \
        --type "String" \
        --description "Promoted image tag for ${service_name} from ${SOURCE_PROJECT_PREFIX}" \
        --overwrite \
        --region "${CDK_AWS_REGION}"

    log_success "Promoted ${service_name} image ${image_tag} from ${src_repo} to ${dst_repo}"

    # Export for downstream workflow steps
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "IMAGE_TAG=${image_tag}" >> "$GITHUB_OUTPUT"
    fi
    echo "IMAGE_TAG=${image_tag}" >> "$GITHUB_ENV"
}

main "$@"
