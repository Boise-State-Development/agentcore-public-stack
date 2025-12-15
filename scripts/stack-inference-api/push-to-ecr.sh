#!/bin/bash
set -euo pipefail

# Script: Push Docker Image to ECR
# Description: Pushes a built Docker image to AWS ECR with versioned tag

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Source common utilities
source "${PROJECT_ROOT}/scripts/common/load-env.sh"

# Logging functions
log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_success() {
    echo "[SUCCESS] $1"
}

# Function to ensure ECR repository exists
ensure_ecr_repo() {
    local repo_name=$1
    local region=$2
    
    log_info "Checking if ECR repository exists: ${repo_name}"
    
    set +e
    aws ecr describe-repositories \
        --repository-names "${repo_name}" \
        --region "${region}" \
        > /dev/null 2>&1
    local exit_code=$?
    set -e
    
    if [ ${exit_code} -ne 0 ]; then
        log_info "ECR repository does not exist. Creating it..."
        aws ecr create-repository \
            --repository-name "${repo_name}" \
            --region "${region}" \
            --image-scanning-configuration scanOnPush=true \
            --encryption-configuration encryptionType=AES256 \
            --tags Key=Project,Value=${CDK_PROJECT_PREFIX} Key=ManagedBy,Value=GitHubActions
        
        # Set lifecycle policy with multi-tier retention
        log_info "Setting lifecycle policy..."
        aws ecr put-lifecycle-policy \
            --repository-name "${repo_name}" \
            --region "${region}" \
            --lifecycle-policy-text '{
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Keep images tagged with latest, deployed, prod, staging, or release tags",
                        "selection": {
                            "tagStatus": "tagged",
                            "tagPrefixList": ["latest", "deployed", "prod", "staging", "v", "release"],
                            "countType": "imageCountMoreThan",
                            "countNumber": 999
                        },
                        "action": {
                            "type": "expire"
                        }
                    },
                    {
                        "rulePriority": 2,
                        "description": "Delete untagged images after 7 days",
                        "selection": {
                            "tagStatus": "untagged",
                            "countType": "sinceImagePushed",
                            "countUnit": "days",
                            "countNumber": 7
                        },
                        "action": {
                            "type": "expire"
                        }
                    }
                ]
            }'
        
        log_success "ECR repository created: ${repo_name}"
    else
        log_info "ECR repository already exists"
        
        # Ensure lifecycle policy exists (in case repo was created without it)
        log_info "Ensuring lifecycle policy is set..."
        aws ecr put-lifecycle-policy \
            --repository-name "${repo_name}" \
            --region "${region}" \
            --lifecycle-policy-text '{
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Keep images tagged with latest, deployed, prod, staging, or release tags",
                        "selection": {
                            "tagStatus": "tagged",
                            "tagPrefixList": ["latest", "deployed", "prod", "staging", "v", "release"],
                            "countType": "imageCountMoreThan",
                            "countNumber": 999
                        },
                        "action": {
                            "type": "expire"
                        }
                    },
                    {
                        "rulePriority": 2,
                        "description": "Delete untagged images after 7 days",
                        "selection": {
                            "tagStatus": "untagged",
                            "countType": "sinceImagePushed",
                            "countUnit": "days",
                            "countNumber": 7
                        },
                        "action": {
                            "type": "expire"
                        }
                    }
                ]
            }' > /dev/null 2>&1 || log_info "Lifecycle policy already exists or was updated"
    fi
}

# Function to push Docker image to ECR
push_to_ecr() {
    local local_image=$1
    local ecr_uri=$2
    local image_tag=$3
    local region=$4
    
    log_info "Pushing ARM64 Docker image to ECR: ${ecr_uri}:${image_tag}"
    
    # Extract account from ECR URI
    local ecr_account=$(echo "${ecr_uri}" | cut -d'.' -f1 | cut -d'/' -f1)
    
    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region "${region}" | \
        docker login --username AWS --password-stdin "${ecr_account}.dkr.ecr.${region}.amazonaws.com"
    
    # Tag image for ECR with version tag
    local remote_image="${ecr_uri}:${image_tag}"
    
    log_info "Tagging image: ${local_image} -> ${remote_image}"
    docker tag "${local_image}" "${remote_image}"
    
    # Push versioned image
    log_info "Pushing versioned image to ECR (this may take several minutes)..."
    docker push "${remote_image}"
    
    # Also tag and push as 'latest' for convenience
    # local latest_image="${ecr_uri}:latest"
    # log_info "Tagging image as latest: ${latest_image}"
    # docker tag "${local_image}" "${latest_image}"
    
    # log_info "Pushing latest image to ECR..."
    # docker push "${latest_image}"
    
    log_success "Docker image pushed successfully to ECR with tags: ${image_tag}, latest"
}

main() {
    log_info "Pushing Inference API Docker image to ECR..."
    
    # Validate required environment variables
    if [ -z "${CDK_AWS_ACCOUNT:-}" ]; then
        log_error "CDK_AWS_ACCOUNT is not set"
        exit 1
    fi
    
    if [ -z "${CDK_AWS_REGION:-}" ]; then
        log_error "CDK_AWS_REGION is not set"
        exit 1
    fi
    
    # Set image name and tag
    IMAGE_NAME="${CDK_PROJECT_PREFIX}-inference-api"
    
    # Use git commit SHA as version tag (required - NOT latest)
    if git rev-parse --git-dir > /dev/null 2>&1; then
        IMAGE_TAG=$(git rev-parse --short HEAD)
    else
        log_error "Not in a git repository. Cannot determine version tag."
        exit 1
    fi
    
    log_info "Version tag: ${IMAGE_TAG}"
    
    # Construct ECR URI
    REPO_NAME="${CDK_PROJECT_PREFIX}-inference-api"
    ECR_URI="${CDK_AWS_ACCOUNT}.dkr.ecr.${CDK_AWS_REGION}.amazonaws.com/${REPO_NAME}"
    
    # Ensure ECR repository exists
    ensure_ecr_repo "${REPO_NAME}" "${CDK_AWS_REGION}"
    
    # Push image to ECR with version tag
    LOCAL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
    push_to_ecr "${LOCAL_IMAGE}" "${ECR_URI}" "${IMAGE_TAG}" "${CDK_AWS_REGION}"
    
    # Store image tag in SSM Parameter Store for CDK to use
    SSM_PARAM_NAME="/${CDK_PROJECT_PREFIX}/inference-api/image-tag"
    log_info "Storing image tag in SSM Parameter: ${SSM_PARAM_NAME}"
    
    aws ssm put-parameter \
        --name "${SSM_PARAM_NAME}" \
        --value "${IMAGE_TAG}" \
        --type "String" \
        --description "Current image tag for Inference API deployed to ECR" \
        --overwrite \
        --region "${CDK_AWS_REGION}"
    
    log_success "Image tag stored in SSM: ${IMAGE_TAG}"
    
    # Output the image tag for use in deploy step
    echo ""
    log_success "Push completed successfully!"
    log_info "Image tag: ${IMAGE_TAG}"
    log_info "Full image URI: ${ECR_URI}:${IMAGE_TAG}"
    log_info "SSM Parameter: ${SSM_PARAM_NAME}"
    echo ""
    echo "IMAGE_TAG=${IMAGE_TAG}" >> $GITHUB_OUTPUT
}

main "$@"
