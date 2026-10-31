#!/bin/bash
set -euo pipefail

# Script: Run the eval harness's nightly subset against a deployed nightly stack
# Description: Seeds the eval user (backend/scripts/seed_eval_user.py), signs in
#              as it through the stack's app-api, runs a subset of the golden
#              set once per case, and scores it against the committed Haiku
#              baseline. See docs/specs/eval-harness.md §10 and
#              docs/testing/smoke-regression.md §5.
#
# Runs AFTER scripts/nightly/e2e-test.sh in the same job, which has already
# added the CloudFront URL to app-api's CORS origins and to the Cognito app
# client's callback list — both of which the scripted Hosted UI login needs.
#
# skill_selection is left out: the nightly stack never runs the bootstrap
# seeding, so the two skills that family loads do not exist there.
#
# Required environment variables:
#   CDK_PROJECT_PREFIX    — CDK project prefix (e.g. nightly-develop)
#   CDK_AWS_REGION        — AWS region
#
# Optional:
#   EVAL_REPORT           — path for the score JSON (default: eval-harness-report.json)
#   EVAL_MODEL_ID         — model under test (default: the committed baseline's Haiku 4.5)
#   EVAL_FAMILIES         — comma-separated families (default below)
#
# Exit status: 0 when no gating family regressed, 1 when one did, 2 for a setup
# problem. The workflow step that calls this is non-blocking: one sample per
# case is a smoke signal, not the release gate (that is k=3 on dev).

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
    for var in CDK_PROJECT_PREFIX CDK_AWS_REGION; do
        if [ -z "${!var:-}" ]; then
            log_error "${var} environment variable is required"
            exit 2
        fi
    done

    local base_url
    base_url=$(get_base_url) || exit 2
    local api_url="${base_url}/api"
    local model="${EVAL_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
    local families="${EVAL_FAMILIES:-tool_selection,document_qa,retention,instruction_following,refusal}"
    local report="${EVAL_REPORT:-${PROJECT_ROOT}/eval-harness-report.json}"
    local out="${PROJECT_ROOT}/eval-harness-run"

    cd "${BACKEND_DIR}"
    export AWS_REGION="${CDK_AWS_REGION}"

    log_info "Seeding the eval user for ${CDK_PROJECT_PREFIX}..."
    if ! uv run python scripts/seed_eval_user.py --prefix "${CDK_PROJECT_PREFIX}" --region "${CDK_AWS_REGION}"; then
        log_error "Could not seed the eval user"
        exit 2
    fi

    log_info "Running ${families} once per case on ${model} via ${api_url}"
    set +e
    uv run python scripts/eval_harness.py run \
        --out "${out}" \
        --auth cognito \
        --base-url "${api_url}" \
        --prefix "${CDK_PROJECT_PREFIX}" \
        --region "${CDK_AWS_REGION}" \
        --model-id "${model}" \
        --family "${families}" \
        --k 1 \
        --yes
    local run_code=$?
    set -e
    if [ ${run_code} -ne 0 ]; then
        log_error "The eval run could not complete (exit ${run_code})"
        exit 2
    fi

    set +e
    uv run python scripts/eval_harness.py score --out "${out}" --json "${report}"
    local score_code=$?
    set -e

    case ${score_code} in
        0) log_success "No gating family regressed against the baseline (report: ${report})" ;;
        1) log_error "A gating family regressed against the baseline (report: ${report})" ;;
        2) log_warn "Could not score: no baseline for ${model}, or the run is incomplete" ;;
        *) log_error "Scoring exited ${score_code}" ;;
    esac
    exit ${score_code}
}

main "$@"
