#!/bin/bash
set -euo pipefail

# Script: Run Playwright E2E Tests Against a Deployed Nightly Stack
# Description: Installs Playwright browsers, resolves the frontend CloudFront
#              URL, and runs the full E2E suite using the CI-specific Playwright
#              config.
#
# Required environment variables:
#   CDK_PROJECT_PREFIX    — CDK project prefix (e.g. nightly-develop)
#   CDK_AWS_REGION        — AWS region for CloudFormation lookups
#   ADMIN_USERNAME        — Cognito admin test account username
#   ADMIN_PASSWORD        — Cognito admin test account password
#   USER_USERNAME         — Cognito regular user test account username
#   USER_PASSWORD         — Cognito regular user test account password
#
# The script resolves the frontend URL from:
#   1. SSM parameter /${CDK_PROJECT_PREFIX}/frontend/url (set by FrontendStack)
#   2. CloudFormation WebsiteUrl output from FrontendStack
#   3. CloudFormation DistributionDomainName output from FrontendStack

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FRONTEND_DIR="${PROJECT_ROOT}/frontend/ai.client"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# ---------------------------------------------------------------------------
# Resolve the frontend URL of the deployed stack (CloudFront / S3)
# ---------------------------------------------------------------------------
get_base_url() {
    # Try SSM parameter first (set by FrontendStack)
    local ssm_key="/${CDK_PROJECT_PREFIX}/frontend/url"
    local frontend_url
    frontend_url=$(aws ssm get-parameter \
        --name "${ssm_key}" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)

    if [ -n "${frontend_url}" ] && [ "${frontend_url}" != "None" ]; then
        # SSM value may already include https:// or may be a bare domain
        if [[ "${frontend_url}" == https://* ]]; then
            echo "${frontend_url}"
        else
            echo "https://${frontend_url}"
        fi
        return 0
    fi

    # Fallback: query CloudFormation WebsiteUrl output from FrontendStack
    local stack_name="${CDK_PROJECT_PREFIX}-FrontendStack"
    frontend_url=$(aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --query "Stacks[0].Outputs[?OutputKey=='WebsiteUrl'].OutputValue" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)

    if [ -n "${frontend_url}" ] && [ "${frontend_url}" != "None" ]; then
        if [[ "${frontend_url}" == https://* ]]; then
            echo "${frontend_url}"
        else
            echo "https://${frontend_url}"
        fi
        return 0
    fi

    # Last resort: query CloudFront distribution domain from FrontendStack
    local cf_domain
    cf_domain=$(aws cloudformation describe-stacks \
        --stack-name "${stack_name}" \
        --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)

    if [ -n "${cf_domain}" ] && [ "${cf_domain}" != "None" ]; then
        echo "https://${cf_domain}"
        return 0
    fi

    log_error "Could not resolve frontend URL from SSM (${ssm_key}) or FrontendStack outputs"
    return 1
}

# ---------------------------------------------------------------------------
# Patch Cognito App Client callback URLs to include the dynamic CloudFront URL
# ---------------------------------------------------------------------------
# The nightly stack has no custom domain, so the CloudFront distribution URL
# changes every run. Cognito rejects OAuth redirects to URLs not in its
# allowlist, so we must add the CloudFront URL before running auth tests.
# ---------------------------------------------------------------------------
patch_cognito_callback_urls() {
    local frontend_url="$1"
    local callback_url="${frontend_url}/api/auth/callback"
    local logout_url="${frontend_url}"

    # Fetch Cognito resource IDs from SSM
    local user_pool_id
    user_pool_id=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id" \
        --query "Parameter.Value" --output text \
        --region "${CDK_AWS_REGION}")

    local client_id
    client_id=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/auth/cognito/bff-app-client-id" \
        --query "Parameter.Value" --output text \
        --region "${CDK_AWS_REGION}")

    log_info "  User Pool ID: ${user_pool_id}"
    log_info "  Client ID:    ${client_id}"

    # Read current app client settings
    local current_config
    current_config=$(aws cognito-idp describe-user-pool-client \
        --user-pool-id "${user_pool_id}" \
        --client-id "${client_id}" \
        --region "${CDK_AWS_REGION}" \
        --output json)

    # Extract existing callback and logout URLs
    local existing_callbacks
    existing_callbacks=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
urls = data['UserPoolClient'].get('CallbackURLs', [])
print('\n'.join(urls))
")

    local existing_logouts
    existing_logouts=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
urls = data['UserPoolClient'].get('LogoutURLs', [])
print('\n'.join(urls))
")

    # Check if the CloudFront callback URL is already present
    if echo "${existing_callbacks}" | grep -qF "${callback_url}"; then
        log_info "  Callback URL already present — skipping patch"
        return 0
    fi

    # Build updated URL lists using python3 for reliable JSON construction
    local callbacks_json
    callbacks_json=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
urls = data['UserPoolClient'].get('CallbackURLs', [])
urls.append('${callback_url}')
print(json.dumps(urls))
")

    local logouts_json
    logouts_json=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
urls = data['UserPoolClient'].get('LogoutURLs', [])
new_url = '${logout_url}'
if new_url not in urls:
    urls.append(new_url)
print(json.dumps(urls))
")

    log_info "  Adding callback URL: ${callback_url}"
    log_info "  Adding logout URL:   ${logout_url}"

    # Extract current OAuth settings to preserve them
    local allowed_flows
    allowed_flows=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
flows = data['UserPoolClient'].get('AllowedOAuthFlows', [])
print(' '.join(flows))
")

    local allowed_scopes
    allowed_scopes=$(echo "${current_config}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
scopes = data['UserPoolClient'].get('AllowedOAuthScopes', [])
print(' '.join(scopes))
")

    # Update the app client with the new callback/logout URLs
    aws cognito-idp update-user-pool-client \
        --user-pool-id "${user_pool_id}" \
        --client-id "${client_id}" \
        --callback-urls "${callbacks_json}" \
        --logout-urls "${logouts_json}" \
        --allowed-o-auth-flows ${allowed_flows} \
        --allowed-o-auth-scopes ${allowed_scopes} \
        --allowed-o-auth-flows-user-pool-client \
        --supported-identity-providers COGNITO \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager > /dev/null

    log_success "  Cognito app client patched successfully"
}

# ---------------------------------------------------------------------------
# Patch App API ECS service env vars for the dynamic CloudFront URL
# ---------------------------------------------------------------------------
# The nightly stack has no custom domain, so the frontend is served from a
# dynamic CloudFront URL that isn't known at CDK deploy time. This function
# patches three env vars in the ECS task definition:
#   - CORS_ORIGINS: append the CloudFront origin so cross-origin requests work
#   - BFF_POST_LOGIN_REDIRECT_URL: set to the CloudFront URL so the OAuth
#     callback redirects the browser to the actual frontend (not localhost)
#   - BFF_AUTH_CALLBACK_URL: set to the CloudFront-fronted callback URL so
#     the BFF sends the correct redirect_uri to Cognito's token endpoint
#
# Steps:
#   1. Reads the current ECS task definition
#   2. Patches CORS_ORIGINS, BFF_POST_LOGIN_REDIRECT_URL, BFF_AUTH_CALLBACK_URL
#   3. Registers a new task definition revision
#   4. Updates the ECS service to use it
#   5. Waits for the service to stabilize
# ---------------------------------------------------------------------------
patch_app_api_cors() {
    local frontend_url="$1"

    # Resolve ECS cluster and service names from SSM
    local cluster_name
    cluster_name=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/network/ecs-cluster-name" \
        --query "Parameter.Value" --output text \
        --region "${CDK_AWS_REGION}")

    local service_name="${CDK_PROJECT_PREFIX}-app-api-service"

    log_info "  Cluster: ${cluster_name}"
    log_info "  Service: ${service_name}"

    # Get the current task definition ARN from the service
    local task_def_arn
    task_def_arn=$(aws ecs describe-services \
        --cluster "${cluster_name}" \
        --services "${service_name}" \
        --query "services[0].taskDefinition" \
        --output text \
        --region "${CDK_AWS_REGION}")

    log_info "  Current task definition: ${task_def_arn}"

    # Get the full task definition
    local task_def_json
    task_def_json=$(aws ecs describe-task-definition \
        --task-definition "${task_def_arn}" \
        --query "taskDefinition" \
        --output json \
        --region "${CDK_AWS_REGION}")

    # Check current CORS_ORIGINS value
    local current_cors
    current_cors=$(echo "${task_def_json}" | python3 -c "
import sys, json
td = json.load(sys.stdin)
for container in td.get('containerDefinitions', []):
    for env in container.get('environment', []):
        if env['name'] == 'CORS_ORIGINS':
            print(env['value'])
            sys.exit(0)
print('')
")

    log_info "  Current CORS_ORIGINS: ${current_cors:-<empty>}"

    # Check if all patches are already applied
    local current_post_login
    current_post_login=$(echo "${task_def_json}" | python3 -c "
import sys, json
td = json.load(sys.stdin)
for container in td.get('containerDefinitions', []):
    for env in container.get('environment', []):
        if env['name'] == 'BFF_POST_LOGIN_REDIRECT_URL':
            print(env['value'])
            sys.exit(0)
print('')
")

    log_info "  Current BFF_POST_LOGIN_REDIRECT_URL: ${current_post_login:-<empty>}"

    # Check current BFF_AUTH_CALLBACK_URL value
    local current_callback_url
    current_callback_url=$(echo "${task_def_json}" | python3 -c "
import sys, json
td = json.load(sys.stdin)
for container in td.get('containerDefinitions', []):
    for env in container.get('environment', []):
        if env['name'] == 'BFF_AUTH_CALLBACK_URL':
            print(env['value'])
            sys.exit(0)
print('')
")

    log_info "  Current BFF_AUTH_CALLBACK_URL: ${current_callback_url:-<empty>}"

    local needs_patch=false
    if ! echo "${current_cors}" | grep -qF "${frontend_url}"; then
        needs_patch=true
    fi
    if [ "${current_post_login}" != "${frontend_url}/" ]; then
        needs_patch=true
    fi
    if [ "${current_callback_url}" != "${frontend_url}/api/auth/callback" ]; then
        needs_patch=true
    fi

    if [ "${needs_patch}" = "false" ]; then
        log_info "  All env vars already patched — skipping"
        return 0
    fi

    # Build new CORS_ORIGINS value
    local new_cors
    if echo "${current_cors}" | grep -qF "${frontend_url}"; then
        new_cors="${current_cors}"
    elif [ -n "${current_cors}" ]; then
        new_cors="${current_cors},${frontend_url}"
    else
        new_cors="${frontend_url}"
    fi

    # The BFF callback URL is fronted by CloudFront at /api/*
    local new_callback_url="${frontend_url}/api/auth/callback"
    local new_post_login_url="${frontend_url}/"

    log_info "  New CORS_ORIGINS: ${new_cors}"
    log_info "  New BFF_AUTH_CALLBACK_URL: ${new_callback_url}"
    log_info "  New BFF_POST_LOGIN_REDIRECT_URL: ${new_post_login_url}"

    # Register a new task definition revision with updated env vars
    local new_task_def
    new_task_def=$(echo "${task_def_json}" | \
        NEW_CORS="${new_cors}" \
        NEW_CALLBACK_URL="${new_callback_url}" \
        NEW_POST_LOGIN_URL="${new_post_login_url}" \
        python3 -c "
import sys, json, os

td = json.load(sys.stdin)
new_cors_value = os.environ['NEW_CORS']
new_callback_url = os.environ['NEW_CALLBACK_URL']
new_post_login_url = os.environ['NEW_POST_LOGIN_URL']

# Env vars to set/update
patches = {
    'CORS_ORIGINS': new_cors_value,
    'BFF_AUTH_CALLBACK_URL': new_callback_url,
    'BFF_POST_LOGIN_REDIRECT_URL': new_post_login_url,
}

for container in td.get('containerDefinitions', []):
    env_list = container.setdefault('environment', [])
    for name, value in patches.items():
        found = False
        for env in env_list:
            if env['name'] == name:
                env['value'] = value
                found = True
                break
        if not found:
            env_list.append({'name': name, 'value': value})

# Build the register-task-definition input (only allowed fields)
register_input = {
    'family': td['family'],
    'containerDefinitions': td['containerDefinitions'],
    'taskRoleArn': td.get('taskRoleArn', ''),
    'executionRoleArn': td.get('executionRoleArn', ''),
    'networkMode': td.get('networkMode', 'awsvpc'),
    'requiresCompatibilities': td.get('requiresCompatibilities', ['FARGATE']),
    'cpu': td.get('cpu', ''),
    'memory': td.get('memory', ''),
}

# Include runtimePlatform only if present (not all task defs have it)
if 'runtimePlatform' in td and td['runtimePlatform']:
    register_input['runtimePlatform'] = td['runtimePlatform']

# Remove empty optional fields
register_input = {k: v for k, v in register_input.items() if v}

print(json.dumps(register_input))
")

    # Write to a temp file — aws cli's file:///dev/stdin is unreliable across environments
    local tmp_file
    tmp_file=$(mktemp /tmp/task-def-XXXXXX.json)
    echo "${new_task_def}" > "${tmp_file}"

    local new_task_def_arn
    new_task_def_arn=$(aws ecs register-task-definition \
        --cli-input-json "file://${tmp_file}" \
        --query "taskDefinition.taskDefinitionArn" \
        --output text \
        --region "${CDK_AWS_REGION}")

    rm -f "${tmp_file}"

    log_info "  Registered new task definition: ${new_task_def_arn}"

    # Update the ECS service to use the new task definition
    aws ecs update-service \
        --cluster "${cluster_name}" \
        --service "${service_name}" \
        --task-definition "${new_task_def_arn}" \
        --force-new-deployment \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager > /dev/null

    log_info "  ECS service update initiated — waiting for stabilization..."

    # Wait for the service to stabilize (new tasks running with updated CORS)
    aws ecs wait services-stable \
        --cluster "${cluster_name}" \
        --services "${service_name}" \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true

    # Verify the service is healthy by hitting the health endpoint
    local alb_url
    alb_url=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/network/alb-url" \
        --query "Parameter.Value" --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)

    if [ -n "${alb_url}" ] && [ "${alb_url}" != "None" ]; then
        log_info "  Verifying App API health after CORS patch..."
        local retries=0
        local max_retries=20
        while [ ${retries} -lt ${max_retries} ]; do
            local status_code
            status_code=$(curl -s -o /dev/null -w "%{http_code}" "${alb_url}/health" --max-time 10 || echo "000")
            if [ "${status_code}" = "200" ]; then
                log_success "  App API healthy after CORS patch (HTTP 200)"
                # Verify the new task is actually serving by checking the
                # redirect_uri in /auth/login. The health check can pass on
                # the new task while the old task is still draining — we need
                # to confirm the BFF env var patch is active.
                local verify_redirect
                verify_redirect=$(curl -s -o /dev/null -w "%{redirect_url}" \
                    "${alb_url}/auth/login" --max-time 10 || true)
                local verify_uri=""
                if [ -n "${verify_redirect}" ] && echo "${verify_redirect}" | grep -qF "redirect_uri="; then
                    verify_uri=$(echo "${verify_redirect}" | python3 -c "
import sys, urllib.parse
url = sys.stdin.read().strip()
parsed = urllib.parse.urlparse(url)
params = urllib.parse.parse_qs(parsed.query)
print(params.get('redirect_uri', [''])[0])
" 2>/dev/null || true)
                fi

                if [ -n "${verify_uri}" ] && [ "${verify_uri}" = "${new_callback_url}" ]; then
                    log_success "  BFF redirect_uri confirmed: ${verify_uri}"
                else
                    log_warn "  Health OK but redirect_uri not yet updated: ${verify_uri:-<empty>}"
                    log_warn "  Expected: ${new_callback_url}"
                    log_warn "  Old task may still be in the target group — waiting..."
                    retries=$((retries + 1))
                    if [ ${retries} -lt ${max_retries} ]; then
                        sleep 15
                        continue
                    fi
                fi

                # Wait for the ALB deregistration delay (30s configured in CDK)
                # to ensure the old task is fully drained and no longer serving
                # requests. Without this, the old task (with a different cookie
                # encryption key) may handle the OAuth callback, producing a
                # cookie that the new task cannot unseal.
                log_info "  Waiting 45s for old task deregistration to complete..."
                sleep 45
                log_info "  Deregistration wait complete — only new task should be serving"
                return 0
            fi
            retries=$((retries + 1))
            if [ ${retries} -lt ${max_retries} ]; then
                log_info "  Health check returned HTTP ${status_code}, retrying in 15s... (${retries}/${max_retries})"
                sleep 15
            fi
        done
        log_warn "  App API health check did not return 200 after ${max_retries} attempts — proceeding anyway"
    fi

    log_success "  App API CORS patched successfully"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log_info "Starting Playwright E2E tests against deployed nightly stack..."

    # --- Validate required env vars ---
    local missing=()
    [ -z "${CDK_PROJECT_PREFIX:-}" ]  && missing+=("CDK_PROJECT_PREFIX")
    [ -z "${CDK_AWS_REGION:-}" ]      && missing+=("CDK_AWS_REGION")
    [ -z "${ADMIN_USERNAME:-}" ]      && missing+=("ADMIN_USERNAME")
    [ -z "${ADMIN_PASSWORD:-}" ]      && missing+=("ADMIN_PASSWORD")
    [ -z "${USER_USERNAME:-}" ]       && missing+=("USER_USERNAME")
    [ -z "${USER_PASSWORD:-}" ]       && missing+=("USER_PASSWORD")

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing required environment variables: ${missing[*]}"
        exit 1
    fi

    # --- Resolve base URL ---
    log_info "Resolving deployed stack URL..."
    local base_url
    base_url=$(get_base_url)
    log_info "Base URL: ${base_url}"

    # --- Verify frontend is reachable ---
    log_info "Verifying frontend is reachable..."
    local response_code
    response_code=$(curl -s -o /dev/null -w "%{http_code}" "${base_url}" --max-time 30 || echo "000")
    if [ "${response_code}" = "000" ]; then
        log_error "Frontend is not reachable at ${base_url} (connection failed)"
        exit 1
    fi
    log_info "Frontend responded with HTTP ${response_code}"

    # --- Patch App API CORS to allow requests from the CloudFront origin ---
    # Only patch BFF env vars if they're still set to localhost defaults.
    # When CDK configured a custom domain, the BFF env vars are already correct
    # and we only need to add the base_url to CORS_ORIGINS.
    log_info "Patching App API env vars (CORS, BFF redirect URLs) for CloudFront..."
    patch_app_api_cors "${base_url}"

    # --- Ensure Cognito allows the callback URL ---
    log_info "Patching Cognito app client with callback URL..."
    patch_cognito_callback_urls "${base_url}"

    # --- Seed bootstrap data (models, tools, roles, quotas) ---
    # The nightly stack deploys fresh empty DynamoDB tables. The e2e tests
    # expect models, tools, and RBAC roles to exist. The bootstrap seed
    # script is idempotent and resolves table names from SSM.
    log_info "Seeding bootstrap data (models, tools, roles, quotas)..."
    pip install boto3 --quiet 2>/dev/null || pip3 install boto3 --quiet 2>/dev/null || true
    bash "${PROJECT_ROOT}/scripts/stack-bootstrap/seed.sh"

    # --- Complete first-boot (create initial admin via the API) ---
    # The first-boot endpoint creates the admin user in Cognito, assigns the
    # system_admin role, and marks the system as bootstrapped. Without this,
    # the frontend redirects all users to the first-boot setup screen.
    # This must run BEFORE seed-e2e-users.sh because seed-e2e-users is
    # idempotent and will simply confirm the user that first-boot created.
    log_info "Completing first-boot via App API..."
    local alb_url
    alb_url=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/network/alb-url" \
        --query "Parameter.Value" --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)

    if [ -n "${alb_url}" ] && [ "${alb_url}" != "None" ]; then
        local admin_email="${ADMIN_USERNAME}@e2e-nightly.local"
        local first_boot_status
        first_boot_status=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "${alb_url}/system/first-boot" \
            -H "Content-Type: application/json" \
            -d "{\"username\": \"${ADMIN_USERNAME}\", \"email\": \"${admin_email}\", \"password\": \"${ADMIN_PASSWORD}\"}" \
            --max-time 30)

        if [ "${first_boot_status}" = "200" ]; then
            log_success "First-boot completed successfully"
        elif [ "${first_boot_status}" = "409" ]; then
            log_info "First-boot already completed — skipping"
        else
            log_warn "First-boot returned HTTP ${first_boot_status} — tests may fail on login"
        fi
    else
        log_warn "Could not resolve ALB URL — skipping first-boot completion"
    fi

    # --- Seed E2E test users in Cognito ---
    # Runs after first-boot so the admin user already exists in Cognito.
    # seed-e2e-users is idempotent: it confirms existing users and sets
    # their passwords to the expected values.
    log_info "Seeding E2E test users in Cognito User Pool..."
    bash "${SCRIPT_DIR}/seed-e2e-users.sh"

    # --- Verify BFF auth configuration ---
    # Smoke-test the BFF login redirect to confirm the patched env vars are
    # active. We verify BOTH through the ALB directly AND through CloudFront.
    # The CloudFront verification is critical: the Playwright tests go through
    # CloudFront, so we must confirm the patched task is actually serving
    # requests that arrive via CloudFront's /api/* behavior.
    if [ -n "${alb_url}" ] && [ "${alb_url}" != "None" ]; then
        local expected_callback="${base_url}/api/auth/callback"

        # --- Verify via ALB (direct) ---
        log_info "Verifying BFF auth login redirect (via ALB)..."
        local login_redirect
        login_redirect=$(curl -s -o /dev/null -w "%{redirect_url}" \
            "${alb_url}/auth/login" --max-time 10 || true)

        if [ -n "${login_redirect}" ]; then
            log_info "  BFF /auth/login redirects to: ${login_redirect:0:120}..."
            if echo "${login_redirect}" | grep -qF "redirect_uri="; then
                local actual_redirect_uri
                actual_redirect_uri=$(echo "${login_redirect}" | python3 -c "
import sys, urllib.parse
url = sys.stdin.read().strip()
parsed = urllib.parse.urlparse(url)
params = urllib.parse.parse_qs(parsed.query)
print(params.get('redirect_uri', [''])[0])
")
                log_info "  redirect_uri in authorize request: ${actual_redirect_uri}"
                if [ "${actual_redirect_uri}" != "${expected_callback}" ]; then
                    log_error "  MISMATCH! Expected: ${expected_callback}"
                    log_error "  BFF_AUTH_CALLBACK_URL patch may not have taken effect."
                    log_error "  This will cause cookies to be set on the wrong domain."
                fi
            fi
        else
            log_warn "  Could not verify BFF login redirect (no redirect URL captured)"
        fi

        # --- Verify via CloudFront (the path Playwright actually takes) ---
        # This is the critical check. The browser goes through CloudFront, so
        # we must confirm that CloudFront is routing to the NEW task (with the
        # patched BFF_AUTH_CALLBACK_URL). If the old task is still draining or
        # CloudFront has a stale connection, this will catch it.
        log_info "Verifying BFF auth login redirect (via CloudFront)..."
        local cf_retries=0
        local cf_max_retries=12
        local cf_verified=false
        while [ ${cf_retries} -lt ${cf_max_retries} ]; do
            local cf_login_redirect
            cf_login_redirect=$(curl -s -o /dev/null -w "%{redirect_url}" \
                "${base_url}/api/auth/login" --max-time 15 || true)

            # Also capture the HTTP status code for diagnostics
            local cf_status_code
            cf_status_code=$(curl -s -o /dev/null -w "%{http_code}" \
                "${base_url}/api/auth/login" --max-time 15 || echo "000")

            if [ -n "${cf_login_redirect}" ] && echo "${cf_login_redirect}" | grep -qF "redirect_uri="; then
                local cf_actual_redirect_uri
                cf_actual_redirect_uri=$(echo "${cf_login_redirect}" | python3 -c "
import sys, urllib.parse
url = sys.stdin.read().strip()
parsed = urllib.parse.urlparse(url)
params = urllib.parse.parse_qs(parsed.query)
print(params.get('redirect_uri', [''])[0])
")
                if [ "${cf_actual_redirect_uri}" = "${expected_callback}" ]; then
                    log_success "  CloudFront redirect_uri verified: ${cf_actual_redirect_uri}"
                    cf_verified=true
                    break
                else
                    log_warn "  CloudFront redirect_uri mismatch (attempt $((cf_retries + 1))/${cf_max_retries}): got ${cf_actual_redirect_uri}"
                    log_warn "  Expected: ${expected_callback}"
                    log_warn "  Old task may still be draining — retrying in 10s..."
                fi
            elif [ -n "${cf_login_redirect}" ]; then
                # Got a redirect but not to Cognito — likely ALB HTTP→HTTPS redirect
                # This indicates CloudFront is connecting to ALB over HTTP and getting
                # a 301 redirect to HTTPS instead of reaching the BFF directly.
                log_warn "  CloudFront /api/auth/login returned HTTP ${cf_status_code} redirect to: ${cf_login_redirect:0:120}"
                log_warn "  This looks like an ALB HTTP→HTTPS redirect. CloudFront may be using HTTP_ONLY protocol."
                log_warn "  Fix: Ensure CDK_CERTIFICATE_ARN is set when deploying FrontendStack so CloudFront uses HTTPS to ALB."
            else
                log_warn "  CloudFront /api/auth/login returned HTTP ${cf_status_code} with no redirect (attempt $((cf_retries + 1))/${cf_max_retries}) — retrying in 10s..."
            fi

            cf_retries=$((cf_retries + 1))
            if [ ${cf_retries} -lt ${cf_max_retries} ]; then
                sleep 10
            fi
        done

        if [ "${cf_verified}" = "false" ]; then
            log_error "  CRITICAL: CloudFront is still routing to the old task after ${cf_max_retries} attempts."
            log_error "  The OAuth callback will land on the wrong domain and cookies will not work."
            log_error "  This is the root cause of the 'cookies on wrong domain' E2E failure."
            # Don't exit — let the tests run so we get diagnostic output from Playwright
        fi

        # Verify /auth/session returns 401 (not 500/503) when no cookie is sent.
        # A 500/503 would indicate the BFF middleware or JWT validator is misconfigured.
        log_info "Verifying BFF /auth/session endpoint is functional..."
        local session_status
        session_status=$(curl -s -o /dev/null -w "%{http_code}" \
            "${alb_url}/auth/session" --max-time 10 || echo "000")
        if [ "${session_status}" = "401" ]; then
            log_info "  /auth/session returns 401 (expected — no cookie sent)"
        else
            log_error "  /auth/session returned HTTP ${session_status} (expected 401)"
            log_error "  This suggests the BFF middleware or JWT validator is broken."
            # Fetch the response body for diagnostics
            local session_body
            session_body=$(curl -s "${alb_url}/auth/session" --max-time 10 || true)
            log_error "  Response body: ${session_body:0:200}"
        fi
    fi

    # --- Change to frontend directory ---
    cd "${FRONTEND_DIR}"

    # --- Check node_modules ---
    if [ ! -d "node_modules" ]; then
        log_error "node_modules not found. Frontend dependencies must be installed first."
        exit 1
    fi

    # --- Install Playwright browsers ---
    log_info "Installing Playwright browsers (chromium only)..."
    npx playwright install --with-deps chromium

    # --- Run E2E tests ---
    log_info "Running Playwright E2E tests..."
    log_info "  Config: playwright.ci.config.ts"
    log_info "  Base URL: ${base_url}"

    export E2E_BASE_URL="${base_url}"
    export CI=true

    # Run tests — allow failure so we can still upload artifacts
    local exit_code=0
    npx playwright test --config=playwright.ci.config.ts || exit_code=$?

    if [ ${exit_code} -eq 0 ]; then
        log_success "All E2E tests passed!"
    else
        log_error "E2E tests failed with exit code: ${exit_code}"
    fi

    # Report location is always relative to the config file
    if [ -d "playwright-report" ]; then
        log_info "HTML report generated: playwright-report/index.html"
    fi

    return ${exit_code}
}

main "$@"
