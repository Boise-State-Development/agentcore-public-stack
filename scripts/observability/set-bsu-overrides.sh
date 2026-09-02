#!/usr/bin/env bash
#
# Boise State's observability override profile.
#
# The committed defaults in infrastructure/lib/config.ts are deliberately
# COST-CONSCIOUS: they are what a fork inherits when it configures nothing, so
# they buy useful alerting at the cheapest setting and leave diagnostic depth
# opt-in.
#
# Boise State is not a typical fork. We author this platform, so we do far more
# diagnostic work than any deployer of it — we need deeper traces, longer log
# retention, and tighter thresholds. Those values belong HERE, in GitHub
# Variables scoped to a GitHub Environment, and NOT in committed code. That
# separation is the whole point: every institution's defaults stay right for
# them, and ours stay right for us, with no `config.production` ternary in
# between.
#
# ─────────────────────────────────────────────────────────────────────────────
# THIS SCRIPT IS NOT RUN BY CI AND MAKES NO AWS CHANGES.
#
# It mutates shared repository configuration (GitHub Variables), which is an
# operator action. Run it deliberately, from a machine authenticated to `gh`
# with repo admin rights. It prints what it will do and requires confirmation.
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage:
#   scripts/observability/set-bsu-overrides.sh --env development [--dry-run]
#   scripts/observability/set-bsu-overrides.sh --env production  [--dry-run]
#
# After running, the values flow:
#   GitHub Variable
#     -> .github/workflows/platform.yml (job-level env:)
#     -> scripts/common/load-env.sh build_cdk_context_params()
#     -> --context observability.<field>=...
#     -> infrastructure/lib/config.ts loadConfig()
#     -> config.observability.<field>
#
# The next platform.yml deploy logs the resolved values ("Observability: ..."),
# which is how you confirm a variable actually took effect rather than being
# accepted and ignored.

set -euo pipefail

ENVIRONMENT=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENVIRONMENT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "${ENVIRONMENT}" ]]; then
    echo "ERROR: --env is required (development or production)" >&2
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# The profiles
# ─────────────────────────────────────────────────────────────────────────────
#
# Only values that DIFFER from the committed default are set. Anything omitted
# intentionally inherits the default, so this file stays a diff rather than a
# duplicate of config.ts that can drift from it.

case "${ENVIRONMENT}" in
development)
    # Dev is where we debug, so it is the most diagnostic-heavy environment.
    # Traces matter more here than cost: dev traffic is a fraction of prod, so
    # even 50% sampling is a small absolute number of traces.
    declare -A OVERRIDES=(
        # 50% of invocations traced (default 1%). Dev volume is low enough that
        # this is affordable and high enough to catch an intermittent fault.
        [CDK_OBSERVABILITY_XRAY_SAMPLING_RATE]="0.5"
        [CDK_OBSERVABILITY_XRAY_SAMPLING_RESERVOIR]="5"
        [CDK_OBSERVABILITY_XRAY_INSIGHTS_NOTIFICATIONS]="true"
        # Full request/response payloads. High volume and a PII surface, which
        # is exactly why it is off by default — acceptable in dev, where the
        # data is ours and the debugging value is highest.
        [CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED]="true"
        # 14 days is enough to debug something from last sprint without paying
        # to keep dev noise for a month.
        [CDK_OBSERVABILITY_LOG_RETENTION_DAYS]="14"
        # Tighter than default so we see problems in dev before prod does.
        [CDK_OBSERVABILITY_AGENTCORE_ERROR_THRESHOLD]="5"
        [CDK_OBSERVABILITY_LAMBDA_ERROR_THRESHOLD]="1"
        [CDK_OBSERVABILITY_ALB_TARGET_5XX_THRESHOLD]="5"
        [CDK_OBSERVABILITY_DYNAMO_THROTTLE_THRESHOLD]="1"
        # Catch prompt-cache regressions at the first sign in dev.
        [CDK_OBSERVABILITY_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD]="5"
        [CDK_OBSERVABILITY_PROMPT_CACHE_WASTED_USD_THRESHOLD]="0.5"
    )
    ;;
production)
    # Prod trades trace volume for retention and signal quality. Sampling is
    # far lower than dev because prod traffic is orders of magnitude larger and
    # X-Ray bills per trace recorded; retention is far longer because a prod
    # incident review can reach back weeks.
    declare -A OVERRIDES=(
        # 10% — ten times the default, a fifth of dev. Enough to characterise
        # latency and catch a recurring fault without paying for every turn.
        [CDK_OBSERVABILITY_XRAY_SAMPLING_RATE]="0.1"
        [CDK_OBSERVABILITY_XRAY_SAMPLING_RESERVOIR]="2"
        [CDK_OBSERVABILITY_XRAY_INSIGHTS_NOTIFICATIONS]="true"
        # Deliberately LEFT OFF in production. These records carry every user's
        # prompt and the model's response verbatim: the highest-volume log
        # source available and a real PII surface. Turn on temporarily, for a
        # specific investigation, then turn off again.
        #   [CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED]="true"
        # 90 days supports month-over-month incident review and quarterly
        # reporting.
        [CDK_OBSERVABILITY_LOG_RETENTION_DAYS]="90"
        # Prod thresholds sit between the defaults and dev's: tight enough to
        # catch a real regression, loose enough not to page on single-request
        # noise at production volume.
        [CDK_OBSERVABILITY_AGENTCORE_ERROR_THRESHOLD]="5"
        [CDK_OBSERVABILITY_LAMBDA_ERROR_THRESHOLD]="3"
        [CDK_OBSERVABILITY_ALB_TARGET_5XX_THRESHOLD]="5"
        [CDK_OBSERVABILITY_ECS_CPU_PERCENT]="75"
        [CDK_OBSERVABILITY_ECS_MEMORY_PERCENT]="80"
    )
    ;;
*)
    echo "ERROR: --env must be 'development' or 'production', got '${ENVIRONMENT}'" >&2
    exit 1
    ;;
esac

# ─────────────────────────────────────────────────────────────────────────────
# Preflight
# ─────────────────────────────────────────────────────────────────────────────

if ! command -v gh &> /dev/null && [[ "${DRY_RUN}" != "true" ]]; then
    echo "ERROR: the GitHub CLI (gh) is required. https://cli.github.com/" >&2
    exit 1
fi

# Auth is only needed to actually write. A dry run must work without it, so the
# profile can be reviewed on any machine — including in a devcontainer that has
# no gh session.
if [[ "${DRY_RUN}" != "true" ]] && ! gh auth status &> /dev/null; then
    echo "ERROR: not authenticated. Run 'gh auth login' first." >&2
    exit 1
fi

REPO_NAME="<not queried in dry run>"
if [[ "${DRY_RUN}" != "true" ]]; then
    REPO_NAME=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo '<unknown>')
fi

echo "Boise State observability overrides"
echo "  environment : ${ENVIRONMENT}"
echo "  repository  : ${REPO_NAME}"
echo "  variables   : ${#OVERRIDES[@]}"
echo
echo "Values to set (anything not listed inherits the cost-conscious default):"
for key in $(printf '%s\n' "${!OVERRIDES[@]}" | sort); do
    printf '  %-58s = %s\n' "${key}" "${OVERRIDES[$key]}"
done
echo

if [[ "${DRY_RUN}" == "true" ]]; then
    echo "DRY RUN — nothing was changed."
    echo
    echo "Equivalent commands:"
    for key in $(printf '%s\n' "${!OVERRIDES[@]}" | sort); do
        echo "  gh variable set ${key} --env ${ENVIRONMENT} --body '${OVERRIDES[$key]}'"
    done
    exit 0
fi

# Mutating shared repository configuration — confirm explicitly.
read -r -p "Set these ${#OVERRIDES[@]} variables on the '${ENVIRONMENT}' environment? [y/N] " reply
if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
    echo "Aborted; nothing was changed."
    exit 0
fi

for key in $(printf '%s\n' "${!OVERRIDES[@]}" | sort); do
    echo "  setting ${key}"
    gh variable set "${key}" --env "${ENVIRONMENT}" --body "${OVERRIDES[$key]}"
done

echo
echo "Done. Verify on the next platform.yml deploy: the synth log prints a line"
echo "beginning 'Observability:' with the RESOLVED values. If a value there does"
echo "not match what you just set, the variable is not reaching --context —"
echo "check that it is listed in the deploy job's job-level env: block in"
echo ".github/workflows/platform.yml (workflow-level env: resolves vars.* to"
echo "empty strings)."
