#!/bin/bash
set -euo pipefail

# Script: Run the eval harness once per case against the development deployment
# Description: Seeds the eval user (backend/scripts/seed_eval_user.py) and its
#              fixture projects and agents (seed_eval_fixtures.py), signs in
#              as it through the deployment's app-api, runs the gating
#              families of the golden set once per case on each model in
#              EVAL_MODEL_IDS, and scores each against its own committed
#              baseline. See
#              docs/specs/eval-harness.md §10 and docs/testing/smoke-regression.md §5.
#
# Called by nightly.yml's eval-develop track against the long-lived dev
# deployment, never an ephemeral stack: dev has the bootstrap-seeded skills
# skill_selection loads, a Cognito app client whose callback list already
# names its own domain, and the deploy the baseline was promoted on.
#
# Seeding is idempotent and runs every time so the eval user's role follows
# the golden set on the checked-out branch: a case that names a new tool is
# granted it the same night, instead of failing until someone re-seeds by hand.
# A re-run reuses the stored password, so the SSM parameter is only read.
#
# Each run first sweeps what an earlier, cancelled run left under the eval user
# (`eval_harness.py cleanup --orphans`, uploads and sessions over an hour old).
#
# general_chat is left out: its cases are scored only by the LLM judges, which
# this run does not invoke (they are advisory and cost more than the turns).
#
# Required environment variables:
#   CDK_PROJECT_PREFIX    — CDK project prefix of the target (e.g. dev-boisestateai-v2)
#   CDK_AWS_REGION        — AWS region
#
# Optional:
#   EVAL_REPORT           — path for the first model's score JSON (default:
#                           eval-harness-report.json); every later model's is
#                           the same name with -<model> before .json, and its
#                           run directory is eval-harness-run-<model>
#   EVAL_MODEL_IDS        — comma-separated models under test, run one after
#                           another (default below: Haiku 5.5, the catalog
#                           default on dev since 2026-11-04, and Opus 5.5, about
#                           a quarter of prod spend). A model needs a catalog row
#                           on the target (an uncataloged id is refused) and a
#                           committed baseline (otherwise its run is not scored
#                           or published). Per night at k=1: Haiku 5.5 ~$0.09,
#                           Opus 5.5 ~$3.13, measured from 2026-11-13's k=3 runs.
#   EVAL_MODEL_ID         — a single model; overrides EVAL_MODEL_IDS
#   EVAL_FAMILIES         — comma-separated families (default below)
#   EVAL_PUBLISH          — "true" to also write the score as a LEDGER#EVAL row
#                           (score --publish, docs/specs/release-ledger.md §5.2).
#                           nightly.yml passes the environment's
#                           CDK_RELEASE_LEDGER_ENABLED, so only a deployment
#                           with the ledger on gets the rows. Best-effort: a
#                           failed publish never changes the exit status.
#
# Exit status: 0 when no gating family regressed on any model, 1 when one did
# on at least one model, 2 for a setup problem or a model that could not be run
# or scored (a regression elsewhere still wins, as the stronger signal). A model
# that fails never stops the next one. The workflow step that calls this is non-blocking: one sample per
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
    local models_csv="${EVAL_MODEL_ID:-${EVAL_MODEL_IDS:-us.anthropic.claude-haiku-5-5,us.anthropic.claude-opus-5-5}}"
    local families="${EVAL_FAMILIES:-tool_selection,document_qa,attachment_tools,project_kb,agent_harness,project_memory,skill_outcome,terminal,usage_patterns,retention,instruction_following,refusal,skill_selection}"
    local report="${EVAL_REPORT:-${PROJECT_ROOT}/eval-harness-report.json}"
    local models=()
    IFS=',' read -r -a models <<< "${models_csv}"

    cd "${BACKEND_DIR}"
    export AWS_REGION="${CDK_AWS_REGION}"

    log_info "Seeding the eval user for ${CDK_PROJECT_PREFIX}..."
    if ! uv run python scripts/seed_eval_user.py --prefix "${CDK_PROJECT_PREFIX}" --region "${CDK_AWS_REGION}"; then
        log_error "Could not seed the eval user"
        exit 2
    fi

    # The fixture projects and agents the project_kb and agent_harness families
    # run against (eval-harness spec D13). Idempotent and digest-keyed: a night
    # with no fixture change costs a few list calls, and one with a change
    # waits for the new files to be ingested (minutes) before the run.
    log_info "Seeding the eval fixture projects and agents..."
    if ! uv run python scripts/seed_eval_fixtures.py --prefix "${CDK_PROJECT_PREFIX}" \
            --region "${CDK_AWS_REGION}" --base-url "${api_url}"; then
        log_error "Could not seed the eval fixtures"
        exit 2
    fi

    # A cancelled run never reaches its own cleanup, and its staged.jsonl goes
    # with the runner, so sweep what earlier runs left behind first. Only what
    # is over an hour old, so a run still in progress elsewhere is never
    # touched. Best-effort: a failed sweep does not stop tonight's run.
    log_info "Sweeping leftovers from earlier runs..."
    uv run python scripts/eval_harness.py cleanup --orphans --prefix "${CDK_PROJECT_PREFIX}" \
        --region "${CDK_AWS_REGION}" --older-than 60 --yes || log_warn "The orphan sweep failed; continuing"

    local worst=0 index=0 model code
    for model in "${models[@]}"; do
        model="${model//[[:space:]]/}"
        [ -n "${model}" ] || continue
        # The first model keeps the historical paths, so the artifact upload in
        # nightly.yml (which reads them by name) still finds tonight's run.
        local out="${PROJECT_ROOT}/eval-harness-run"
        local model_report="${report}"
        if [ ${index} -gt 0 ]; then
            local slug="${model//[^A-Za-z0-9]/-}"
            out="${PROJECT_ROOT}/eval-harness-run-${slug}"
            model_report="${report%.json}-${slug}.json"
        fi
        index=$((index + 1))
        set +e
        run_model "${model}" "${families}" "${api_url}" "${out}" "${model_report}"
        code=$?
        set -e
        if [ ${code} -eq 1 ]; then
            worst=1
        elif [ ${code} -ne 0 ] && [ ${worst} -eq 0 ]; then
            worst=2
        fi
    done
    if [ ${index} -eq 0 ]; then
        log_error "No model to run (EVAL_MODEL_IDS is empty)"
        exit 2
    fi
    exit ${worst}
}

# One model: run the families once per case, then score (and, with
# EVAL_PUBLISH=true, publish) against that model's committed baseline.
# Returns 0 (no gating regression), 1 (a regression) or 2 (could not run or score).
run_model() {
    local model="$1" families="$2" api_url="$3" out="$4" report="$5"

    log_info "Running ${families} once per case on ${model} via ${api_url}"
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
    if [ ${run_code} -ne 0 ]; then
        log_error "The eval run on ${model} could not complete (exit ${run_code})"
        return 2
    fi

    local publish_args=()
    if [ "${EVAL_PUBLISH:-}" = "true" ]; then
        publish_args=(--publish --track nightly)
        log_info "Publishing the ${model} score to the release ledger"
    fi

    uv run python scripts/eval_harness.py score --out "${out}" --json "${report}" ${publish_args[@]+"${publish_args[@]}"}
    local score_code=$?

    case ${score_code} in
        0) log_success "${model}: no gating family regressed against the baseline (report: ${report})" ;;
        1) log_error "${model}: a gating family regressed against the baseline (report: ${report})" ;;
        2) log_warn "${model}: could not score: no baseline for it, or the run is incomplete" ;;
        *) log_error "${model}: scoring exited ${score_code}" ;;
    esac
    case ${score_code} in
        0|1) return ${score_code} ;;
        *) return 2 ;;
    esac
}

main "$@"
