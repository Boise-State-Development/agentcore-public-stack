#!/bin/bash
set -euo pipefail

# Script: Run the turn-path smoke matrix against a deployed nightly stack
# Description: Drives real agent turns through the stack's app-api (via the
#              CloudFront /api path, exactly as the SPA does) as the nightly
#              E2E user, and asserts the response contract on the wire: SSE
#              frame order, interrupt/resume, the single-flight 409, Stop,
#              preview sessions, attachments, restore, cache-hash stability.
#              See docs/testing/smoke-regression.md §2 for the rows.
#
# Two callers. nightly-deploy-pipeline.yml runs it AFTER
# scripts/nightly/e2e-test.sh in the same job, as the E2E user, against an
# ephemeral stack (e2e-test.sh has already added the CloudFront URL to app-api's
# CORS origins and to the Cognito app client's callback list, which the
# scripted Hosted UI login needs). nightly.yml's eval track runs it against the
# long-lived dev deployment as the eval user (TURN_SMOKE_AUTH=eval-user), and
# that run publishes to the release ledger: the ephemeral stack's ledger is
# deleted with the stack at teardown, so a row written there is read by nobody.
#
# Required environment variables:
#   CDK_PROJECT_PREFIX    — CDK project prefix (e.g. nightly-develop)
#   CDK_AWS_REGION        — AWS region for the SSM lookup
#   USER_USERNAME         — Cognito regular-user test account (permanent
#   USER_PASSWORD           password); not needed with TURN_SMOKE_AUTH=eval-user
#
# Optional:
#   TURN_SMOKE_AUTH       — "eval-user" to sign in as the user
#                           backend/scripts/seed_eval_user.py seeded on this
#                           deployment (credentials from its SSM parameter)
#   TURN_SMOKE_PUBLISH    — "true" to write the result as a LEDGER#SMOKE row
#                           (smoke_turns.py --publish, release-ledger spec §5.2);
#                           best-effort, never changes the exit status
#   TURN_SMOKE_REPORT     — path for the JSON report (default: turn-smoke-report.json)
#   TURN_SMOKE_EXTRA_ARGS — appended to the smoke_turns.py invocation
#
# Exit status is the script's: 0 when no row FAILED, 1 otherwise, 2 for a
# setup problem (no base URL, login refused). The workflow step that calls this
# is non-blocking until the matrix has a track record on the nightly stack.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# Same primary lookup e2e-test.sh uses: the SSM parameter the SPA distribution
# construct writes. No CloudFormation fallback here — if the parameter is
# missing, the E2E step before this one has already failed loudly.
get_base_url() {
    local frontend_url
    frontend_url=$(aws ssm get-parameter \
        --name "/${CDK_PROJECT_PREFIX}/frontend/url" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || true)
    if [ -z "${frontend_url}" ] || [ "${frontend_url}" = "None" ]; then
        log_error "Could not resolve the frontend URL from SSM (/${CDK_PROJECT_PREFIX}/frontend/url)"
        return 1
    fi
    if [[ "${frontend_url}" == https://* ]]; then
        echo "${frontend_url}"
    else
        echo "https://${frontend_url}"
    fi
}

main() {
    local required=(CDK_PROJECT_PREFIX CDK_AWS_REGION)
    if [ "${TURN_SMOKE_AUTH:-}" != "eval-user" ]; then
        required+=(USER_USERNAME USER_PASSWORD)
    fi
    for var in "${required[@]}"; do
        if [ -z "${!var:-}" ]; then
            log_error "${var} environment variable is required"
            exit 2
        fi
    done

    local base_url
    base_url=$(get_base_url) || exit 2
    local api_url="${base_url}/api"
    log_info "Project prefix: ${CDK_PROJECT_PREFIX}"
    log_info "app-api via CloudFront: ${api_url}"

    local report="${TURN_SMOKE_REPORT:-${PROJECT_ROOT}/turn-smoke-report.json}"

    local auth_args=()
    if [ "${TURN_SMOKE_AUTH:-}" = "eval-user" ]; then
        # The script reads the eval user's credentials from SSM itself.
        auth_args=(--auth eval-user --prefix "${CDK_PROJECT_PREFIX}" --region "${CDK_AWS_REGION}")
        log_info "Signing in as the eval user"
    else
        # The script reads the credentials from SMOKE_USERNAME / SMOKE_PASSWORD so
        # they never appear on a command line.
        export SMOKE_USERNAME="${USER_USERNAME}"
        export SMOKE_PASSWORD="${USER_PASSWORD}"
        auth_args=(--auth cognito)
    fi
    local publish_args=()
    if [ "${TURN_SMOKE_PUBLISH:-}" = "true" ]; then
        publish_args=(--publish --prefix "${CDK_PROJECT_PREFIX}" --region "${CDK_AWS_REGION}")
        log_info "Publishing the result to the release ledger"
    fi

    cd "${BACKEND_DIR}"
    export AWS_REGION="${CDK_AWS_REGION}"
    # shellcheck disable=SC2086 — TURN_SMOKE_EXTRA_ARGS is deliberately word-split
    set +e
    uv run python scripts/smoke_turns.py \
        --base-url "${api_url}" \
        "${auth_args[@]}" \
        ${publish_args[@]+"${publish_args[@]}"} \
        --with-attachments \
        --json "${report}" \
        ${TURN_SMOKE_EXTRA_ARGS:-}
    local exit_code=$?
    set -e

    case ${exit_code} in
        0) log_success "Turn smoke matrix passed (report: ${report})" ;;
        1) log_error "Turn smoke matrix has failing rows (report: ${report})" ;;
        2) log_warn "Turn smoke matrix could not start — a scripted Hosted UI login needs a classic (server-rendered) login page and a user with a permanent password" ;;
        *) log_error "Turn smoke matrix exited ${exit_code}" ;;
    esac
    exit ${exit_code}
}

main "$@"
