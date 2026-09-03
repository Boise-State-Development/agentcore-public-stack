# Load-test provisioning

Creates and destroys the Cognito users a load run needs. The load test itself
lives in [`tests/load/`](../../tests/load/) and has no AWS credentials — these
scripts are the only part that touches your account.

> **Not run by CI, and not safe to wire into a workflow.** Both scripts mutate
> live shared state: users are created in the same pool real people sign in to,
> and `provision.sh` writes quota overrides that **switch off cost limits** for
> those users. Same posture as `scripts/observability/set-bsu-overrides.sh` —
> confirmation required, `--dry-run` available, never automated.

## Why provisioning is needed at all

Two of the platform's own safety rails block a load test, by design:

1. **`FORCE_CHANGE_PASSWORD` blocks scripted login.** A freshly created Cognito
   user cannot complete the Hosted UI form until it has a permanent password.
2. **Per-user cost quotas hard-stop sustained traffic.** Without an override the
   run stops measuring the chat path and starts measuring quota enforcement.

Neither can be worked around from the test side, which is why this is a separate
step with its own gate rather than something the locustfile does.

## Usage

```bash
export CDK_PROJECT_PREFIX=your-prefix
export CDK_AWS_REGION=us-west-2
export AWS_PROFILE=your-profile   # the devcontainer sets AWS_REGION but no profile

# See the plan without changing anything
scripts/load-test/provision.sh --users 10 --quota-days 1 --dry-run

# Do it
scripts/load-test/provision.sh --users 10 --quota-days 1
```

Both scripts print the resolved AWS account before the plan and again in the
plan itself. **Read it.** The prefix alone does not tell you which account you
are aimed at, and these scripts create real users and switch off real cost
controls.

If `AWS_PROFILE` is unset, every call falls through to the default credential
chain, which in the devcontainer is not your SSO session.

`provision.sh` prints the exact environment exports for the run when it
finishes. Then, when the run is done:

```bash
scripts/load-test/teardown.sh --manifest ~/.config/agentcore-load/users-<run-id>.json
```

Run inside the devcontainer — the scripts need `aws`, `jq`, `openssl` and GNU `date`.

## Watching the Bedrock quota during a run

```bash
scripts/load-test/watch-tpm.sh --model-id global.anthropic.claude-sonnet-5
```

Read-only. Polls CloudWatch `EstimatedTPMQuotaUsage` in five-minute windows and
prints tokens/min, percent of the **applied** quota, turns/min, and implied
tokens/turn. It warns when the applied quota equals the AWS default, since that
means no increase has ever landed — the usual reason a capacity plan assumes
headroom it does not have.

The quota code is discovered from the model id: a `global.` prefix resolves to
the Global cross-region limit, `us.`/`eu.`/`apac.` to the plain cross-region one.
Pass `--quota-code` if discovery is ambiguous, or `--quota N` to skip the lookup.

Five-minute windows rather than one-minute are deliberate: a single observed
production minute reported 546,206 quota tokens against one invocation, which
exceeds that model's own context window, so per-minute peaks are not trustworthy.

This lives here rather than in `tests/load/` because the load generator has no
AWS credentials by design.

## The manifest

`provision.sh` writes a `0600` JSON file, by default under
`~/.config/agentcore-load/`, deliberately outside the repo tree:

```json
[
  {
    "username": "loadtest-20260902-221500-01",
    "password": "...",
    "user_id": "<cognito sub>",
    "override_id": "loadtest-20260902-221500-1"
  }
]
```

It holds **plaintext passwords and is the only copy** — Cognito will not show
them again. `teardown.sh` needs it to know what to delete, and deletes it
afterwards unless you pass `--keep-manifest`.

## What gets created

Per user:

| Step | Call | Note |
|---|---|---|
| User | `cognito-idp admin-create-user` | `MessageAction=SUPPRESS`, so no mail is sent. The email attribute is required by the pool, so one is set at `@load.invalid` — non-routable per RFC 6761 — with `email_verified=true`. |
| Password | `cognito-idp admin-set-user-password --permanent` | Moves the user to `CONFIRMED` from any state. |
| Quota override | `dynamodb put-item` | An `unlimited` override, time-bounded by `--quota-days`. |

The override is written straight to the quota table rather than through the
admin API, because the admin API needs an authenticated admin session — which
would mean solving the login problem to solve the login problem. The item
mirrors `QuotaRepository.create_override`: `PK=OVERRIDE#<id>`, `SK=METADATA`,
plus the `GSI4PK`/`GSI4SK` pair that `get_active_override` queries. It is keyed
on the Cognito **`sub`**, since that is what the app uses as `user_id`.

## Safety rails

- **Username prefix check.** Every entry in a manifest must start with
  `loadtest-`, verified across the whole file *before* anything is deleted. A
  hand-edited or swapped manifest cannot become a tool for deleting real users,
  and cannot delete a subset before failing.
- **Overrides are deleted before users.** If teardown is interrupted, the
  leftover you want is not "a live account with no cost limit."
- **Teardown fails loudly on a stuck override.** It keeps the manifest and exits
  non-zero, because a silently retained override is a disabled cost control.
- **Credentials never pass through argv.** `ps` is world-readable; passwords go
  via `0600` temp files and `--cli-input-json`.
- **Re-runnable.** An existing user is not an error; the password is reset and
  the override rewritten.

## If teardown fails

Overrides are visible in the admin dashboard under **Quota Overrides** and can
be removed there. Users are removable with `admin-delete-user`. Confirm nothing
is left behind with:

```bash
aws cognito-idp list-users \
  --user-pool-id "$(aws ssm get-parameter \
      --name "/${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id" \
      --query Parameter.Value --output text)" \
  --filter 'username ^= "loadtest-"' \
  --query 'Users[].Username'
```
