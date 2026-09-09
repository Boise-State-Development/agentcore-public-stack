#!/usr/bin/env bash
# Watch Bedrock TPM quota consumption while a load test runs.
#
#   scripts/load-test/watch-tpm.sh --model-id global.anthropic.claude-sonnet-5
#
# WHY THIS IS A SEPARATE SCRIPT
#
# tests/load/ deliberately has no AWS credentials — provisioning is the only
# part of this system that touches your account, and that boundary is worth
# keeping. So the quota readout runs alongside the load generator rather than
# inside it. It is read-only: CloudWatch GetMetricStatistics and Service Quotas
# reads, nothing else.
#
# WHAT IT TELLS YOU THAT THE LOCUST OUTPUT CANNOT
#
#  * Quota headroom. Throttling arrives as opaque 5xx/error turns in Locust.
#    Here you see the approach to the limit before it bites, which is the
#    difference between "we found the ceiling" and "the run mysteriously broke".
#  * Whether the run is token-REPRESENTATIVE. It divides quota tokens by
#    invocations to give implied tokens/turn. Production measures ~26,700; the
#    default (tool-less, short-prompt) load profile produces ~1,920. If this
#    column reads two thousand, the run is not testing what you think, and no
#    amount of user count will make it so.
#
# WHY 5-MINUTE WINDOWS
#
# Single-minute EstimatedTPMQuotaUsage datapoints are not trustworthy: one
# observed minute in production reported 546,206 quota tokens against a single
# invocation, which exceeds the model's own 200k context window. Whether that is
# stream-window attribution or cache-write accounting, it makes per-minute peaks
# unusable. Five-minute sums divided by five are stable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"

MODEL_ID=""
QUOTA_CODE=""
QUOTA_VALUE=""
INTERVAL=60
WINDOW=300

usage() {
    cat <<'EOF'
Usage: watch-tpm.sh --model-id ID [options]

  --model-id ID      CloudWatch ModelId dimension. This is the inference profile,
                     not the bare model, e.g.:
                       global.anthropic.claude-sonnet-5      (production default)
                       us.anthropic.claude-haiku-4-5-20251001-v1:0   (dev default)
  --quota-code CODE  Service Quotas code for that model's TPM limit. Discovered
                     automatically when omitted; pass explicitly if discovery is
                     ambiguous (e.g. L-DD84E5CA for Sonnet 5 global).
  --quota N          Skip the Service Quotas lookup and use this limit.
  --interval N       Seconds between samples (default 60).
  --window N         Metric window in seconds; must be a multiple of 60
                     (default 300).
  -h, --help         This message

Ctrl-C to stop. Read-only; makes no changes.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --model-id)   MODEL_ID="$2"; shift 2 ;;
        --quota-code) QUOTA_CODE="$2"; shift 2 ;;
        --quota)      QUOTA_VALUE="$2"; shift 2 ;;
        --interval)   INTERVAL="$2"; shift 2 ;;
        --window)     WINDOW="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -z "${MODEL_ID}" ]; then
    log_error "--model-id is required."
    usage
    exit 1
fi
for pair in "INTERVAL:${INTERVAL}" "WINDOW:${WINDOW}"; do
    name="${pair%%:*}"
    value="${pair#*:}"
    if ! [[ "${value}" =~ ^[0-9]+$ ]] || [ "${value}" -lt 1 ]; then
        log_error "--${name,,} must be a positive integer (got '${value}')."
        exit 1
    fi
done
if [ $((WINDOW % 60)) -ne 0 ]; then
    log_error "--window must be a multiple of 60 (got ${WINDOW})."
    exit 1
fi

# This script does not resolve SSM parameters, so it needs credentials and a
# region but not a project prefix. Set a placeholder so require_env's shared
# check passes without inventing a prefix requirement that means nothing here.
CDK_PROJECT_PREFIX="${CDK_PROJECT_PREFIX:-n/a}"
require_env

# ---------------------------------------------------------------------------
# Resolve the quota limit
# ---------------------------------------------------------------------------

# Turn an inference-profile id into the label Service Quotas uses.
#   global.anthropic.claude-sonnet-5             -> 'sonnet 5'
#   us.anthropic.claude-haiku-4-5-20251001-v1:0  -> 'haiku 4.5'
# The digit-hyphen-digit rule matters: quota names spell versions with a dot
# ('Haiku 4.5') while model ids use hyphens, so a naive tr would produce
# 'haiku 4 5' and match nothing.
_quota_label_for_model() {
    printf '%s' "$1" \
        | sed -E '
            s/^(global|us|eu|apac)\.//
            s/^anthropic\.claude-//
            s/-[0-9]{8}-v[0-9]+:[0-9]+$//
            s/([0-9])-([0-9])/\1.\2/g
        ' \
        | tr '-' ' '
}

discover_quota_code() {
    local label scope matches count
    label="$(_quota_label_for_model "${MODEL_ID}")"

    # The id prefix selects the quota scope, and there is one of each per model:
    # 'global.' bills against the Global cross-region limit, 'us.'/'eu.'/'apac.'
    # against the plain cross-region one. Without this both match and the user is
    # asked to disambiguate something the model id already stated.
    case "${MODEL_ID}" in
        global.*) scope="global cross-region" ;;
        *)        scope="cross-region" ;;
    esac

    log_info "Discovering TPM quota for '${label}' (${scope})..."

    matches="$(aws service-quotas list-service-quotas \
        --service-code bedrock \
        --max-items 500 \
        --region "${CDK_AWS_REGION}" \
        --output json 2>/dev/null \
        | jq -r --arg label "${label}" --arg scope "${scope}" '
            .Quotas[]
            | . as $q
            | ($q.QuotaName | ascii_downcase) as $name
            | select($name | contains("tokens per minute"))
            | select($name | contains($label | ascii_downcase))
            | select(
                if $scope == "global cross-region"
                then ($name | startswith("global cross-region"))
                else ($name | startswith("cross-region"))
                end
              )
            | "\($q.QuotaCode)\t\($q.QuotaName)\t\($q.Value)"' || echo "")"

    count="$(printf '%s' "${matches}" | grep -c . || true)"
    if [ "${count}" -eq 0 ]; then
        log_error "No '${scope} ... tokens per minute' quota matched label '${label}'."
        log_error "Pass --quota-code explicitly, or --quota N to skip the lookup."
        exit 1
    fi
    if [ "${count}" -gt 1 ]; then
        log_error "Label '${label}' matched ${count} quotas; pass --quota-code to disambiguate:"
        printf '%s\n' "${matches}" | while IFS=$'\t' read -r code name value; do
            log_error "  ${code}  ${name} = ${value}"
        done
        exit 1
    fi

    QUOTA_CODE="$(printf '%s' "${matches}" | cut -f1)"
    QUOTA_VALUE="$(printf '%s' "${matches}" | cut -f3)"
    log_info "Using ${QUOTA_CODE}: $(printf '%s' "${matches}" | cut -f2)"
}

if [ -z "${QUOTA_VALUE}" ]; then
    if [ -z "${QUOTA_CODE}" ]; then
        discover_quota_code
    else
        QUOTA_VALUE="$(aws service-quotas get-service-quota \
            --service-code bedrock \
            --quota-code "${QUOTA_CODE}" \
            --query "Quota.Value" \
            --output text \
            --region "${CDK_AWS_REGION}" 2>/dev/null || echo "")"
        if [ -z "${QUOTA_VALUE}" ] || [ "${QUOTA_VALUE}" = "None" ]; then
            log_error "Could not read quota ${QUOTA_CODE}. Pass --quota N instead."
            exit 1
        fi
    fi
fi

# The APPLIED value is what throttles you. Report when it equals the AWS
# default, because that means no increase has ever landed — the single most
# common reason a capacity plan silently assumes headroom it does not have.
if [ -n "${QUOTA_CODE}" ]; then
    default_value="$(aws service-quotas get-aws-default-service-quota \
        --service-code bedrock \
        --quota-code "${QUOTA_CODE}" \
        --query "Quota.Value" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || echo "")"
    if [ -n "${default_value}" ] && [ "${default_value}" = "${QUOTA_VALUE}" ]; then
        log_warn "Applied quota equals the AWS default (${QUOTA_VALUE}) — no increase is in effect."
    fi
fi

# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

_metric_sum() {
    # Most recent complete datapoint for a metric over the window.
    local metric="$1" start end
    end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    start="$(date -u -d "-$((WINDOW * 2)) seconds" +%Y-%m-%dT%H:%M:%SZ)"

    aws cloudwatch get-metric-statistics \
        --namespace AWS/Bedrock \
        --metric-name "${metric}" \
        --dimensions "Name=ModelId,Value=${MODEL_ID}" \
        --start-time "${start}" \
        --end-time "${end}" \
        --period "${WINDOW}" \
        --statistics Sum \
        --region "${CDK_AWS_REGION}" \
        --output json 2>/dev/null \
        | jq -r '[.Datapoints[]] | sort_by(.Timestamp) | last | .Sum // 0' 2>/dev/null \
        || echo "0"
}

log_info "Watching ${MODEL_ID} against a ${QUOTA_VALUE} tokens/min quota."
log_info "Sampling every ${INTERVAL}s over ${WINDOW}s windows. Ctrl-C to stop."
echo
printf '%-10s %14s %8s %10s %14s %s\n' \
    "TIME" "TOKENS/MIN" "% QUOTA" "TURNS/MIN" "TOKENS/TURN" "STATUS"

while true; do
    quota_tokens="$(_metric_sum EstimatedTPMQuotaUsage)"
    invocations="$(_metric_sum Invocations)"

    read -r tpm pct tps per_turn status <<EOF
$(awk -v tokens="${quota_tokens}" \
      -v invs="${invocations}" \
      -v window="${WINDOW}" \
      -v quota="${QUOTA_VALUE}" '
    BEGIN {
        minutes = window / 60
        tpm = tokens / minutes
        tps = invs / minutes
        pct = (quota > 0) ? (tpm / quota) * 100 : 0
        per_turn = (invs > 0) ? tokens / invs : 0

        # Thresholds mirror the leading-vs-lagging split in the observability
        # doc: act on the approach, not on the throttle.
        status = "ok"
        if (pct >= 90)      status = "CRITICAL-throttling-imminent"
        else if (pct >= 70) status = "WARN-request-increase-now"
        else if (pct >= 50) status = "watch"

        # Flag an unrepresentative workload. Production is ~26,700 tokens/turn;
        # anything under ~10,000 means tools are off or prompts are trivial, and
        # the TPM result does not generalise.
        if (invs > 0 && per_turn < 10000) status = status "/UNREPRESENTATIVE"

        printf "%.0f %.1f %.1f %.0f %s", tpm, pct, tps, per_turn, status
    }')
EOF

    printf '%-10s %14s %7s%% %10s %14s %s\n' \
        "$(date -u +%H:%M:%S)" "${tpm}" "${pct}" "${tps}" "${per_turn}" "${status}"

    sleep "${INTERVAL}"
done
