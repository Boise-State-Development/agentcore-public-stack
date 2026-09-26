# Load-test provisioning

Creates the Cognito users a load run needs, and destroys them again together
with everything the application wrote for them. The load test itself lives in
[`tests/load/`](../../tests/load/) and has no AWS credentials — these scripts
are the only part that touches your account.

> **Not run by CI, and not safe to wire into a workflow.** Both scripts mutate
> live shared state: users are created in the same pool real people sign in to,
> `provision.sh` writes quota overrides that **switch off cost limits** for
> those users, and `teardown.sh` deletes per-user rows across the app's tables. Same posture as `scripts/observability/set-bsu-overrides.sh` —
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

Teardown reads everything it will delete first and prints it as the plan —
per user, counts of session rows, cost rows, rollup markers, memory records and
so on — then asks. `--dry-run` stops after the plan. `--jobs N` (default 8) sets
how many users are processed concurrently.

Run inside the devcontainer — the scripts need `aws` (2.34+, for
`bedrock-agentcore`), `jq`, `openssl`, GNU `date` and bash 4.3+.

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

### By `provision.sh`, per user

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

### By the application, once a load-test user signs in and chats

Everything below is keyed on the same `sub`, and none of it existed until the
load run logged in. `teardown.sh` deletes all of it
([`app-data.sh`](app-data.sh)); earlier versions deleted only the two rows above,
which is how 620 load-test users came to be counted as real users in
production's admin dashboards.

| Store | What a load run writes | How teardown finds it |
|---|---|---|
| `<prefix>-users` | `USER#<sub>` / `PROFILE`, on the login callback | Query `PK = USER#<sub>` — read first (owner check), deleted **last** |
| `<prefix>-sessions-metadata` | Session rows `S#…`, per-call cost rows `C#…`, and `D#`, `F#`, `TSUM#`, `APPCARD#`, `UIRES#`, `LEASE#` rows | Query `PK = USER#<sub>` — every user row lives in that partition |
| `<prefix>-user-cost-summary` | `USER#<sub>` / `PERIOD#YYYY-MM` — what `PeriodCostIndex` lists as the month's users | Query `PK = USER#<sub>` |
| `<prefix>-system-cost-rollup` | Unique-user markers `ACTIVE#DAILY#<date>`, `ACTIVE#MONTHLY#<period>`, `ACTIVE#MODEL#<period>#<model>` with the sub as SK, each of which bumped an `activeUsers`/`uniqueUsers` counter | One filtered scan per run (the sub is the *sort* key, so no query can find it) — see [Rollups](#what-happens-to-system-cost-rollup) |
| `<prefix>-quota-events` | `warning` / `block` / `session_notice` events, if an override expired mid-run | Query `PK = USER#<sub>` |
| `<prefix>-user-quotas` | The provisioned override, plus any from a re-provision | By id from the manifest, and `UserOverrideIndex` (`GSI4PK = USER#<sub>`) |
| `<prefix>-user-file-uploads` + bucket | `FILE#` / `QUOTA` rows and `user-files/<sub>/…` objects when a tool writes a document (the campus profile enables `create_word_document`); `compaction-offload/<sub>/…` for offloaded tool results | Query `PK = USER#<sub>`; one delimiter listing per bucket prefix per run |
| `<prefix>-user-artifacts` + `artifacts-content` bucket | `ARTIFACT#…` rows and `<sub>/…` objects when `create_artifact` fires | Query `PK = USER#<sub>`; one delimiter listing per run |
| AgentCore Memory (long-term) | Facts, preferences and summaries extracted from the conversation, under `/strategies/<id>/actors/<sub>/` | `list-memory-records` per strategy, `batch-delete-memory-records` |

Measured in dev (2026-09-23) on users from a 2026-09-03 run: each had ~22
session rows, 1 cost-summary row, 3 rollup markers, 8–9 memory records and an
expired override — and none had been removed by the old teardown.

**Deliberately not touched**, because a load run cannot write them or they
clean themselves up:

| Store | Why teardown leaves it |
|---|---|
| AgentCore Memory **events** (raw turns) | Expire on the memory's 90-day `eventExpiryDuration`. Deleting them means one `delete-event` call per event (tens per user); nothing reads them once the session rows are gone, and no dashboard counts them. Long-term records do *not* expire, which is why those are deleted. |
| `<prefix>-bff-sessions` | TTL'd, and locust's `on_stop` logs out. No index on `user_id`, so finding one would be a full scan. |
| `<prefix>-user-settings`, app-roles `TOOL_PREFERENCES` / `SKILL_PREFERENCES` | Written only by explicit `PUT`s. `GET /users/me/settings` (the read-only scenario) returns defaults without writing. |
| `<prefix>-api-keys`, `<prefix>-oauth-user-tokens` | Need `POST /auth/api-keys` or an explicit OAuth disconnect; the scenarios call neither. |
| `<prefix>-memory-spaces`, `<prefix>-shared-conversations`, `<prefix>-rag-assistants`, fine-tuning tables, announcement acks | Each needs a create/share/ack action no scenario performs. |
| `<prefix>-audit-log`, direct quota assignments | Written by admin actions only. |

If a scenario ever starts exercising one of these, add it to `app-data.sh` —
the inventory, the plan line and the delete step — in the same PR.

### What happens to `system-cost-rollup`

The rollup rows carry two kinds of number, and teardown treats them differently:

- **Population counters are corrected.** Each `ACTIVE#…` marker is deleted in
  the same `TransactWriteItems` that decrements the `activeUsers` (daily,
  monthly) or `uniqueUsers` (per model) counter it once incremented. The marker
  delete is conditional on the marker existing, so a re-run cancels instead of
  decrementing twice; a conflict with live cost writes is retried. Without
  this, the admin cost page's "active users" — read straight from
  `ROLLUP#MONTHLY.activeUsers` — would still count every load-test user, since
  nothing in the codebase recomputes it and the table has no TTL.
- **Cost, token and request totals are kept.** Load-test turns were real
  Bedrock invocations, billed to the account. Subtracting them would make the
  rollups disagree with Cost Explorer and with the `PLATFORM#` rows
  platform-cost-sync writes from it, and there is no way to put the history
  back afterwards.

The consequence is stated rather than hidden: after teardown, a period's
rollup total exceeds the sum of its `user-cost-summary` rows by exactly the
load-test spend, and the dashboard's cost-per-user is that spend divided over
the real users. The plan prints the amount per period —

```
  Load-test spend removed from <prefix>-user-cost-summary but KEPT in
  <prefix>-system-cost-rollup totals (it was real Bedrock spend — see README):
    2026-09  $67.61  across 310 user(s)
```

— so the gap can be explained when someone reconciles the two.

## Safety rails

- **Username prefix check.** Every entry in a manifest must start with
  `loadtest-`, verified across the whole file *before* anything is deleted. A
  hand-edited or swapped manifest cannot become a tool for deleting real users,
  and cannot delete a subset before failing.
- **Sub shape check.** Every `user_id` must be a well-formed Cognito sub, also
  across the whole file first. The sub is interpolated into keys and S3
  prefixes; an empty one would make `user-files/<sub>/` a prefix over everyone.
- **Owner check on the users row.** Before any app data is touched, each sub's
  `<prefix>-users` row must carry the load-test email: exactly
  `<username>@<domain>` in manifest mode, `loadtest-…@<domain>` in orphan mode
  (`--email-domain`, default `load.invalid`). One mismatch refuses the whole
  run, so a swapped sub — even one swapped between two load-test users — can
  never delete a real user's data. A sub with no users row never signed in and
  has no app data to delete.
- **Overrides are deleted before users.** If teardown is interrupted, the
  leftover you want is not "a live account with no cost limit."
- **The users row is deleted last,** and only when every other step for that
  user succeeded. It is the evidence the owner check reads, so a re-run can
  always verify a partially torn-down user again.
- **Teardown fails loudly on anything stuck.** A failed override delete, row
  batch, S3 prefix, memory batch or rollup transaction exits non-zero and keeps
  the manifest. A silently retained override is a disabled cost control, and a
  silently retained row is a phantom user.
- **Re-runnable.** Deleting what is already gone is a no-op, and a rollup
  marker that is already gone cancels its transaction rather than decrementing
  a counter twice. Verified in dev by injecting a failure mid-teardown: the run
  exited 1 with the manifest and users row kept, and a plain re-run finished.
- **`--orphans` is a dry run by default,** and skips any sub that still has a
  Cognito account — that is a live run or a run with a manifest, which belongs
  to `--manifest`.
- **Credentials never pass through argv.** `ps` is world-readable; passwords go
  via `0600` temp files and `--cli-input-json`.
- **Provisioning is re-runnable.** An existing user is not an error; the
  password is reset and the override rewritten.

## Cleaning up after runs whose manifest is gone

A manifest is deleted by a successful teardown and is otherwise the only record
of a run, so runs torn down by an older `teardown.sh` — which removed the
Cognito user and override but not the app data — can no longer be cleaned up
through `--manifest`. `--orphans` finds them from the app side instead:

```bash
# Plan only (the default): what would be removed, and the spend kept in rollups
scripts/load-test/teardown.sh --orphans

# A small batch first, then the rest
scripts/load-test/teardown.sh --orphans --apply --limit 20
scripts/load-test/teardown.sh --orphans --apply
```

It queries `<prefix>-users` through `EmailDomainIndex` for
`DOMAIN#load.invalid`, then keeps only rows that

1. have a well-formed sub, `PK = USER#<sub>`, and an email shaped like
   `loadtest-…@load.invalid`, and
2. have **no Cognito account** for that sub. The pool is listed once, unfiltered
   and paginated, and each candidate is looked up in that set — a per-sub
   `sub = "…"` filter gives the same answer but ran at ~26 subs a minute
   against the production pool. A sub that still has an account is reported as
   "still in Cognito" and left for `--manifest`; an empty listing is treated as
   a failed read, never as "everyone is orphaned".

Each survivor then goes through the same owner check, inventory, plan and
delete as a manifest entry — with no Cognito or override-by-id step, since
those are already gone (any override still pointing at the sub is found through
`UserOverrideIndex` and removed with the app data).

Interrupting it is safe: users rows go last, so the next run picks up exactly
where this one stopped. At `--jobs 8` expect roughly 15–30 minutes per 600
users; nearly all of it is AWS CLI start-up (~15 calls per user).

As of 2026-09-22 production holds 620 such users (runs of 2026-09-10 and
2026-09-11), 310 of them with September cost rows. **Run `--orphans` against
production yourself, dry run first**; nothing in CI or in this change does.

## If teardown fails

Re-run it — with the kept manifest, or `--orphans --apply` — and it resumes.
Overrides are also visible in the admin dashboard under **Quota Overrides** and
can be removed there. Confirm no Cognito users are left behind with:

```bash
aws cognito-idp list-users \
  --user-pool-id "$(aws ssm get-parameter \
      --name "/${CDK_PROJECT_PREFIX}/auth/cognito/user-pool-id" \
      --query Parameter.Value --output text)" \
  --filter 'username ^= "loadtest-"' \
  --query 'Users[].Username'
```

and no app data with `teardown.sh --orphans`, which should report
`0 orphaned`.
