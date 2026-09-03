#!/usr/bin/env bash
# Provision Cognito users + quota overrides for a load-test run.
#
#   scripts/load-test/provision.sh --users 10 --quota-days 1
#
# NOT RUN BY CI, and deliberately so. This mutates live shared state in the
# target account: it creates Cognito users in the same pool real people sign in
# to, and it writes quota overrides that DISABLE COST CONTROLS for those users.
# Same posture as scripts/observability/set-bsu-overrides.sh — confirmation
# required, --dry-run available, never wired into a workflow.
#
# What it does, per user:
#   1. cognito-idp admin-create-user      (MessageAction=SUPPRESS, no mail sent)
#   2. cognito-idp admin-set-user-password --permanent
#        Required: FORCE_CHANGE_PASSWORD blocks scripted Hosted-UI login.
#   3. dynamodb put-item -> an 'unlimited' quota override, time-bounded
#        Required: sustained turns otherwise trip the per-user cost limit and
#        the run measures quota enforcement instead of the chat path.
#
# Output is a 0600 manifest consumed by tests/load via
# AGENTCORE_LOAD_USERS_FILE. It contains PLAINTEXT PASSWORDS — it is the only
# copy, it is not in the repo tree by default, and teardown.sh needs it.
#
# Required environment:
#   CDK_PROJECT_PREFIX   resolves SSM parameters
#   CDK_AWS_REGION       (or AWS_REGION)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"

USER_COUNT=10
QUOTA_DAYS=1
RUN_ID=""
MANIFEST=""
DRY_RUN=false
ASSUME_YES=false

usage() {
    cat <<'EOF'
Usage: provision.sh [options]

  --users N          Number of Cognito users to create (default 10)
  --quota-days N     Days the unlimited quota override stays valid (default 1)
  --run-id ID        Tag for this batch; defaults to a UTC timestamp
  --manifest PATH    Where to write credentials
                     (default ~/.config/agentcore-load/users-<run-id>.json)
  --email-domain D   Domain for the required email attribute
                     (default load.invalid — intentionally non-routable)
  --dry-run          Print the plan; read SSM but make no changes
  --yes              Skip the confirmation prompt (for a trusted wrapper)
  -h, --help         This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --users)        USER_COUNT="$2"; shift 2 ;;
        --quota-days)   QUOTA_DAYS="$2"; shift 2 ;;
        --run-id)       RUN_ID="$2"; shift 2 ;;
        --manifest)     MANIFEST="$2"; shift 2 ;;
        --email-domain) LOAD_TEST_EMAIL_DOMAIN="$2"; shift 2 ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --yes)          ASSUME_YES=true; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if ! [[ "${USER_COUNT}" =~ ^[0-9]+$ ]] || [ "${USER_COUNT}" -lt 1 ]; then
    log_error "--users must be a positive integer (got '${USER_COUNT}')"
    exit 1
fi
if ! [[ "${QUOTA_DAYS}" =~ ^[0-9]+$ ]] || [ "${QUOTA_DAYS}" -lt 1 ]; then
    log_error "--quota-days must be a positive integer (got '${QUOTA_DAYS}')"
    exit 1
fi

require_env
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
validate_run_id "${RUN_ID}"
MANIFEST="${MANIFEST:-${HOME}/.config/agentcore-load/users-${RUN_ID}.json}"

# ---------------------------------------------------------------------------
# Resolve targets. Reads only — safe in --dry-run, and doing it before the
# prompt means the plan shown is the plan that will run.
# ---------------------------------------------------------------------------
USER_POOL_ID="$(resolve_user_pool_id)"
QUOTA_TABLE="$(resolve_quota_table)"
COGNITO_DOMAIN_URL="$(resolve_cognito_domain_url "${USER_POOL_ID}")"

cat <<EOF

$(log_plan "Load-test provisioning plan")
  Project prefix   : ${CDK_PROJECT_PREFIX}
  Region           : ${CDK_AWS_REGION}
  User pool        : ${USER_POOL_ID}
  Quota table      : ${QUOTA_TABLE}
  Hosted UI        : ${COGNITO_DOMAIN_URL}
  Run ID           : ${RUN_ID}
  Users to create  : ${USER_COUNT}  (${USERNAME_PREFIX}${RUN_ID}-01 .. $(printf '%s%s-%02d' "${USERNAME_PREFIX}" "${RUN_ID}" "${USER_COUNT}"))
  Email domain     : ${LOAD_TEST_EMAIL_DOMAIN}
  Quota override   : unlimited, valid ${QUOTA_DAYS} day(s)
  Manifest         : ${MANIFEST}

EOF

log_warn "This creates real users in a live pool and disables their cost limits."
log_warn "Run teardown.sh when the test finishes, or the overrides outlive it."

if [ "${DRY_RUN}" = true ]; then
    log_info "--dry-run: no changes made."
    exit 0
fi

confirm_or_exit "${ASSUME_YES}" "Create ${USER_COUNT} users and ${USER_COUNT} unlimited quota overrides?"

# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "${MANIFEST}")"
chmod 700 "$(dirname "${MANIFEST}")" 2>/dev/null || true

# Create the manifest empty and locked down BEFORE writing any password into
# it, so there is no window where it exists world-readable.
: > "${MANIFEST}"
chmod 600 "${MANIFEST}"

VALID_FROM="$(app_timestamp 0)"
VALID_UNTIL="$(app_timestamp "${QUOTA_DAYS}")"

entries=()
created=0

for index in $(seq 1 "${USER_COUNT}"); do
    username="$(printf '%s%s-%02d' "${USERNAME_PREFIX}" "${RUN_ID}" "${index}")"
    email="${username}@${LOAD_TEST_EMAIL_DOMAIN}"
    password="$(generate_password)"
    # Guards the [A-Za-z0-9!] invariant that keeps the value safe to embed in
    # JSON and shell without escaping. See generate_password.
    assert_safe_password "${password}"

    log_info "[${index}/${USER_COUNT}] ${username}"

    # MessageAction=SUPPRESS: no invitation mail. email_verified=true so the
    # pool's autoVerify never tries to reach the non-routable address.
    if ! aws cognito-idp admin-create-user \
            --user-pool-id "${USER_POOL_ID}" \
            --username "${username}" \
            --user-attributes "Name=email,Value=${email}" "Name=email_verified,Value=true" \
            --message-action SUPPRESS \
            --region "${CDK_AWS_REGION}" \
            --no-cli-pager >/dev/null 2>&1; then
        # Already existing is fine and makes the script re-runnable; anything
        # else is not, so surface it by retrying loudly.
        log_warn "  admin-create-user failed; user may already exist — continuing"
    fi

    # Unconditional: moves the user to CONFIRMED from any state. Without this,
    # Hosted-UI login hits FORCE_CHANGE_PASSWORD and the load test cannot log in.
    # Goes through a 0600 temp file so the password never appears in argv.
    set_permanent_password "${USER_POOL_ID}" "${username}" "${password}"

    # The app keys everything on the Cognito `sub` claim
    # (cognito_jwt_validator.py: user_id=payload["sub"]), so the override must
    # be written against the sub, not the username.
    user_id="$(aws cognito-idp admin-get-user \
        --user-pool-id "${USER_POOL_ID}" \
        --username "${username}" \
        --query "UserAttributes[?Name=='sub'].Value | [0]" \
        --output text \
        --region "${CDK_AWS_REGION}")"

    if [ -z "${user_id}" ] || [ "${user_id}" = "None" ]; then
        log_error "  Could not read the 'sub' attribute for ${username}; skipping override"
        continue
    fi

    override_id="loadtest-${RUN_ID}-${index}"
    put_unlimited_override \
        "${QUOTA_TABLE}" "${override_id}" "${user_id}" \
        "${VALID_FROM}" "${VALID_UNTIL}" "${RUN_ID}"

    entries+=("$(json_entry "${username}" "${password}" "${user_id}" "${override_id}")")
    created=$((created + 1))
    log_success "  ready (sub ${user_id:0:8}…, override ${override_id})"
done

if [ "${created}" -eq 0 ]; then
    log_error "No users were provisioned. Manifest left empty: ${MANIFEST}"
    exit 1
fi

{
    # jq -s slurps the individual entry objects into a single array, so the
    # manifest is valid JSON without hand-assembling commas.
    printf '%s\n' "${entries[@]}" | jq -s '.'
} > "${MANIFEST}"
chmod 600 "${MANIFEST}"

cat <<EOF

$(log_success "Provisioned ${created} user(s).")

Manifest (0600, contains plaintext passwords — the only copy):
  ${MANIFEST}

Run the load test with:

  export AGENTCORE_LOAD_COGNITO_DOMAIN="${COGNITO_DOMAIN_URL}"
  export AGENTCORE_LOAD_USERS_FILE="${MANIFEST}"
  cd tests/load && uv run locust -f locustfile.py --host https://<your-domain>/api

Then clean up — the quota overrides are live cost controls that are currently OFF:

  scripts/load-test/teardown.sh --manifest "${MANIFEST}"

EOF
