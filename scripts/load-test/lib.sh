#!/usr/bin/env bash
# Shared helpers for scripts/load-test/. Sourced, not executed.
#
# Design notes worth knowing before editing:
#
#  * No Python. The devcontainer has no system interpreter at all — only
#    uv-managed ones, off PATH — so these scripts use `jq` and GNU `date`,
#    which are present and are the right tools for the job anyway.
#  * Credentials never travel through argv. `ps` is world-readable and this
#    mints many long-lived passwords at once, so every AWS call carrying a
#    password does it through a 0600 temp file and --cli-input-json.

# Usernames all share this prefix. teardown.sh refuses to act on a manifest
# containing anything else, so a hand-edited manifest cannot be turned into a
# tool for deleting real users.
#
# shellcheck disable=SC2034  # consumed by provision.sh / teardown.sh after sourcing
USERNAME_PREFIX="loadtest-"

# Non-routable by design (RFC 6761 reserves .invalid), so a misconfigured pool
# can never deliver mail to a real inbox.
LOAD_TEST_EMAIL_DOMAIN="${LOAD_TEST_EMAIL_DOMAIN:-load.invalid}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_plan()    { echo -e "${BLUE}[PLAN]${NC} $1"; }

# Temp files hold credentials; make sure they never survive the process.
_LOAD_TEST_TMPDIR=""
_cleanup_tmp() {
    if [ -n "${_LOAD_TEST_TMPDIR}" ] && [ -d "${_LOAD_TEST_TMPDIR}" ]; then
        rm -rf "${_LOAD_TEST_TMPDIR}"
    fi
}
trap _cleanup_tmp EXIT INT TERM

_tmpdir() {
    if [ -z "${_LOAD_TEST_TMPDIR}" ]; then
        _LOAD_TEST_TMPDIR="$(mktemp -d)"
        chmod 700 "${_LOAD_TEST_TMPDIR}"
    fi
    printf '%s' "${_LOAD_TEST_TMPDIR}"
}

require_env() {
    local missing=()
    [ -z "${CDK_PROJECT_PREFIX:-}" ] && missing+=("CDK_PROJECT_PREFIX")

    # Accept the usual region variables so this works with an already-configured
    # shell or the devcontainer's AWS_REGION.
    CDK_AWS_REGION="${CDK_AWS_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
    [ -z "${CDK_AWS_REGION}" ] && missing+=("CDK_AWS_REGION (or AWS_REGION)")

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing required environment variables: ${missing[*]}"
        exit 1
    fi
    export CDK_AWS_REGION

    for tool in aws jq openssl date; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            log_error "${tool} is required but not on PATH. Run inside the devcontainer."
            exit 1
        fi
    done

    # The timestamp format below needs GNU date's %N and -d. BSD/macOS date
    # silently produces something else, which would break the string-compared
    # validUntil.
    if ! date -u -d "+1 day" +%N >/dev/null 2>&1; then
        log_error "GNU date is required (BSD/macOS date lacks -d and %N)."
        exit 1
    fi

    require_credentials
}

# Fail on credentials before the plan prints, and name the account.
#
# Two reasons this is worth its own preflight rather than letting the first API
# call fail. The devcontainer sets AWS_REGION but no AWS_PROFILE, so an
# unexported profile sends every call to whatever the default chain resolves —
# which surfaced as a bogus "is your prefix correct?" on the first SSM read.
# And this script creates users in a live pool and switches off their cost
# limits, so which account it is aimed at is the single most important thing to
# state out loud before doing any of that.
require_credentials() {
    local identity
    local err
    err="$(_tmpdir)/sts-err"

    if ! identity="$(aws sts get-caller-identity \
        --query "Account" --output text \
        --region "${CDK_AWS_REGION}" 2>"${err}")"; then
        log_error "AWS credentials are not usable."
        if [ -s "${err}" ]; then
            log_error "AWS said: $(tr '\n' ' ' <"${err}")"
        fi
        log_error "Set AWS_PROFILE (the devcontainer does not set one) or run 'aws sso login'."
        exit 1
    fi

    export LOAD_TEST_AWS_ACCOUNT="${identity}"
    log_info "AWS account ${identity}, region ${CDK_AWS_REGION}, profile ${AWS_PROFILE:-<default chain>}"
}

# Run IDs end up inside usernames and DynamoDB keys, so constrain them rather
# than interpolating arbitrary input.
validate_run_id() {
    if ! [[ "$1" =~ ^[A-Za-z0-9-]+$ ]]; then
        log_error "Run ID must match ^[A-Za-z0-9-]+\$ (got '$1')"
        exit 1
    fi
}

confirm_or_exit() {
    local assume_yes="$1"
    local prompt="$2"

    if [ "${assume_yes}" = true ]; then
        log_warn "--yes given; skipping confirmation."
        return 0
    fi

    local reply
    read -r -p "$(echo -e "${YELLOW}${prompt}${NC} [y/N] ")" reply
    case "${reply}" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) log_info "Aborted."; exit 0 ;;
    esac
}

_ssm_value() {
    local name="$1"
    local value
    local err
    err="$(_tmpdir)/ssm-err"

    value="$(aws ssm get-parameter \
        --name "${name}" \
        --query "Parameter.Value" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>"${err}" || echo "")"

    if [ -z "${value}" ] || [ "${value}" = "None" ]; then
        log_error "Could not resolve SSM parameter: ${name}"
        # Print what AWS actually said. ExpiredToken, AccessDenied and
        # ParameterNotFound are three different problems with three different
        # fixes, and discarding stderr made them indistinguishable — every one
        # of them read as "your prefix is wrong".
        if [ -s "${err}" ]; then
            log_error "AWS said: $(tr '\n' ' ' <"${err}")"
        fi
        log_error "Prefix '${CDK_PROJECT_PREFIX}', region '${CDK_AWS_REGION}'."
        exit 1
    fi
    printf '%s' "${value}"
}

resolve_user_pool_id() {
    _ssm_value "/${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id"
}

resolve_quota_table() {
    _ssm_value "/${CDK_PROJECT_PREFIX}/quota/user-quotas-table-name"
}

# Derived from the pool rather than read from SSM: CDK computes the Hosted UI
# URL at synth time for the app-api environment and does not publish it as a
# parameter, so asking Cognito is the only source that cannot drift.
resolve_cognito_domain_url() {
    local user_pool_id="$1"
    local domain_prefix
    domain_prefix="$(aws cognito-idp describe-user-pool \
        --user-pool-id "${user_pool_id}" \
        --query "UserPool.Domain" \
        --output text \
        --region "${CDK_AWS_REGION}" 2>/dev/null || echo "")"

    if [ -z "${domain_prefix}" ] || [ "${domain_prefix}" = "None" ]; then
        log_error "User pool ${user_pool_id} has no Hosted UI domain."
        log_error "Scripted login needs the Hosted UI; see tests/load/README.md."
        exit 1
    fi
    printf 'https://%s.auth.%s.amazoncognito.com' "${domain_prefix}" "${CDK_AWS_REGION}"
}

# Emit a timestamp shaped exactly like the one the application compares against.
#
# `QuotaRepository.get_active_override` builds its comparison value as
# `datetime.now(timezone.utc).isoformat() + 'Z'`, which yields a
# microsecond-precision '+00:00Z' suffix, and then compares GSI4SK *as a
# string*. `%6N+00:00Z` reproduces that byte for byte; a plain `date -u ...Z`
# would emit a different suffix and could sort wrong at a boundary.
app_timestamp() {
    local days_ahead="${1:-0}"
    date -u -d "+${days_ahead} days" +%Y-%m-%dT%H:%M:%S.%6N+00:00Z
}

# 24 chars with all four character classes, so any reasonable pool password
# policy is satisfied without having to read the policy.
#
# Restricted to [A-Za-z0-9] plus the fixed 'Lt' prefix and '9!' suffix. That is
# not only about policy: it keeps the value free of anything needing JSON or
# shell escaping. `assert_safe_password` enforces the invariant so a future
# change to this function cannot silently corrupt a manifest.
generate_password() {
    local body
    body="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 20)"
    printf 'Lt%s9!' "${body}"
}

assert_safe_password() {
    if ! [[ "$1" =~ ^[A-Za-z0-9!]+$ ]]; then
        log_error "Generated password contains unexpected characters."
        log_error "generate_password must stay within [A-Za-z0-9!] — see its comment."
        exit 1
    fi
}

# Write an 'unlimited' quota override. Key layout mirrors
# QuotaRepository.create_override exactly — PK/SK plus the GSI4 pair that
# get_active_override queries through the UserOverrideIndex.
put_unlimited_override() {
    local table="$1" override_id="$2" user_id="$3"
    local valid_from="$4" valid_until="$5" run_id="$6"

    local item_file
    item_file="$(_tmpdir)/override-${override_id}.json"

    ( umask 077
      jq -n \
        --arg overrideId "${override_id}" \
        --arg userId "${user_id}" \
        --arg validFrom "${valid_from}" \
        --arg validUntil "${valid_until}" \
        --arg runId "${run_id}" \
        '{
          PK:           {S: ("OVERRIDE#" + $overrideId)},
          SK:           {S: "METADATA"},
          GSI4PK:       {S: ("USER#" + $userId)},
          GSI4SK:       {S: ("VALID_UNTIL#" + $validUntil)},
          overrideId:   {S: $overrideId},
          userId:       {S: $userId},
          overrideType: {S: "unlimited"},
          validFrom:    {S: $validFrom},
          validUntil:   {S: $validUntil},
          reason:       {S: ("Load test run " + $runId + " (scripts/load-test/provision.sh)")},
          createdBy:    {S: "load-test-provisioner"},
          createdAt:    {S: $validFrom},
          enabled:      {BOOL: true}
        }' > "${item_file}"
    )

    aws dynamodb put-item \
        --table-name "${table}" \
        --item "file://${item_file}" \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager >/dev/null

    rm -f "${item_file}"
}

delete_override() {
    local table="$1" override_id="$2"
    local key_file
    key_file="$(_tmpdir)/key-${override_id}.json"

    jq -n --arg overrideId "${override_id}" \
        '{PK: {S: ("OVERRIDE#" + $overrideId)}, SK: {S: "METADATA"}}' > "${key_file}"

    aws dynamodb delete-item \
        --table-name "${table}" \
        --key "file://${key_file}" \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager >/dev/null

    local status=$?
    rm -f "${key_file}"
    return "${status}"
}

# Password goes via a 0600 file, not argv — see the note at the top.
set_permanent_password() {
    local user_pool_id="$1" username="$2" password="$3"

    local input_file
    input_file="$(_tmpdir)/pw-${username}.json"

    ( umask 077
      jq -n \
        --arg pool "${user_pool_id}" \
        --arg user "${username}" \
        --arg pass "${password}" \
        '{UserPoolId: $pool, Username: $user, Password: $pass, Permanent: true}' \
        > "${input_file}"
    )

    aws cognito-idp admin-set-user-password \
        --cli-input-json "file://${input_file}" \
        --region "${CDK_AWS_REGION}" \
        --no-cli-pager >/dev/null

    rm -f "${input_file}"
}

# JSON-encode one manifest entry. jq handles the escaping that hand-rolled
# string interpolation would get wrong.
json_entry() {
    jq -n \
        --arg username "$1" \
        --arg password "$2" \
        --arg user_id "$3" \
        --arg override_id "$4" \
        '{username: $username, password: $password, user_id: $user_id, override_id: $override_id}'
}
