---
title: Upgrading from Multi-Stack
description: Migrate an older 9-stack deployment to the single PlatformStack.
sidebar:
  order: 8
---

Earlier versions of the platform split infrastructure across nine separate
CloudFormation stacks. The current architecture consolidates all of it into one
— `PlatformStack`. If you're running an older multi-stack deployment, this page
walks the migration: back up, tear down, redeploy, and restore.

:::danger[This is a destructive migration]
The old stacks are deleted and replaced. Data is preserved through the
backup/restore workflows, but the steps must run in order — and a bad backup
means data loss. Read the whole page before starting.
:::

## Before you start

- [ ] Admin access to the GitHub repository (to trigger workflows).
- [ ] The latest code on `main` (or the branch carrying the single-stack
      architecture). Confirm `infrastructure/lib/platform-stack.ts` exists and
      `infrastructure/bin/infrastructure.ts` references only `PlatformStack`.
- [ ] Your environment variables and secrets configured (see
      [Platform (CDK)](/agentcore-public-stack/deployment/platform-cdk/#github-configuration)).
- [ ] Your `CDK_PROJECT_PREFIX` and the target GitHub environment in hand.

## 1. Back up

This is the **most critical step** — it captures all application data so it can
be restored after the rebuild.

Run **Actions → Backup Data (Pre-Migration)** with:

| Input | Value |
|-------|-------|
| `project_prefix` | Your `CDK_PROJECT_PREFIX` |
| `aws_region` | Your AWS region |
| `aws_environment` | The GitHub environment (e.g. `production`) |
| `include_ephemeral` | `false` — session tables aren't worth preserving |

Note the **backup bucket name** from the output (`{prefix}-backup-{timestamp}`)
and confirm the logs show a non-zero `summary.ok` count and **zero**
`summary.failed`.

:::caution
Do not proceed if the backup fails or reports any failed components. Fix the
cause and re-run the backup first.
:::

## 2. Tear down

Run **Actions → Teardown All Infrastructure** with:

| Input | Value |
|-------|-------|
| `environment` | The environment to tear down |
| `confirm` | `DESTROY` (exactly, all caps) |

This destroys the existing CloudFormation stacks in that environment.

## 3. Clean up retained resources

If the environment had `CDK_RETAIN_DATA_ON_DELETE=true` (the old production
default), CloudFormation **retained** stateful resources instead of deleting
them. They must be removed before the new stack can recreate replacements with
the same names.

Typically retained: the ~24 DynamoDB tables, the data S3 buckets, the Cognito
User Pool, Secrets Manager secrets, KMS keys, and the SSM parameters under
`/${CDK_PROJECT_PREFIX}/`.

:::caution[The image-tag parameters break the first new deploy]
`/${CDK_PROJECT_PREFIX}/{app-api,inference-api}/image-tag` will still hold a
legacy *tag-only* value (a git short SHA) from your last multi-stack deploy. The
new architecture expects these to be **full ECR URIs**, and the first
`PlatformStack` deploy fails CloudFormation early-validation if the legacy value
is present:

```
Property value [<short-sha>] does not match pattern:
  ^\d{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/...
```

The seed step in `scripts/platform/deploy.sh` repairs this automatically on the
next deploy, but you can also delete the two parameters by hand below.
:::

Identify and delete the retained resources:

```bash
# Identify
aws dynamodb list-tables --query "TableNames[?starts_with(@, '${CDK_PROJECT_PREFIX}')]"
aws s3 ls | grep "${CDK_PROJECT_PREFIX}"
aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n'

# Delete the image-tag params (minimum required for an in-place migration)
aws ssm delete-parameter --name "/${CDK_PROJECT_PREFIX}/app-api/image-tag"       2>/dev/null || true
aws ssm delete-parameter --name "/${CDK_PROJECT_PREFIX}/inference-api/image-tag" 2>/dev/null || true

# ...or sweep every parameter under your prefix (safe — all re-published on the next deploy)
aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive \
  --query 'Parameters[].Name' --output text \
  | tr '\t' '\n' | xargs -r -n10 aws ssm delete-parameters --names
```

Delete DynamoDB tables, empty and remove S3 buckets (`aws s3 rb … --force`), the
Cognito pool, and the secrets the same way.

:::tip
Setting `CDK_RETAIN_DATA_ON_DELETE=false` **before** teardown lets CloudFormation
delete everything automatically and skips this step. Only do that if you're
confident your backup is good.
:::

:::caution
Do **not** delete the backup bucket (`{prefix}-backup-{timestamp}`) — you need it
for the restore. It survives all teardown operations.
:::

## 4. Deploy the new architecture

With the old stacks gone and retained resources cleared, deploy the single stack,
then ship code and data:

1. **Platform Stack** — `cdk deploy` provisions all infrastructure (~15 min).
2. **Backend Deploy** — builds images, pushes to ECR, updates ECS / Lambda / Runtime.
3. **Frontend Deploy** — publishes the Angular SPA.
4. **Bootstrap Data Seeding** — seeds default configuration.

See [Deployment Overview](/agentcore-public-stack/deployment/overview/#first-time-deploy-order)
for the full first-deploy sequence.

## 5. Restore

Run **Actions → Restore Data** with:

| Input | Value |
|-------|-------|
| `backup_bucket` | The bucket from step 1 |
| `manifest_key` | `{prefix}/{timestamp}/manifest.json` |
| `target_prefix` | Your `CDK_PROJECT_PREFIX` (unchanged) |
| `region` | Your AWS region |
| `dry_run` | `true` first, then `false` |
| `skip_cognito_users` | `false` (unless you want users to re-register) |

Run with `dry_run=true` first and review the output to confirm it found your
data, then run again with `dry_run=false` to write it into the new tables and
buckets.

## 6. Verify

Visit the application and confirm login works, chat history is present, file
uploads are accessible, the admin dashboard shows users / costs / models, and RAG
assistants work. Confirm every workflow on the Actions dashboard is green.

## Rollback

If the migration fails and you need to revert:

1. Run **Teardown** to destroy the new stack.
2. Switch the repository back to the old multi-stack branch.
3. Redeploy the old architecture via its workflows.
4. Restore data from the same backup bucket.

The backup bucket is immutable and survives all teardown operations.

## Timeline

| Step | Duration |
|------|----------|
| Backup | 5–15 min |
| Teardown | 5–10 min |
| Clean up retained resources | 5–15 min (skipped if `retainDataOnDelete=false`) |
| Platform deploy | 10–15 min |
| Backend deploy | 3–5 min |
| Frontend deploy | 2 min |
| Bootstrap seeding | 1 min |
| Restore | 5–15 min |
| **Total** | **~45–75 min** |

## Migration gotchas

- **Cognito passwords don't transfer.** AWS doesn't export password hashes —
  native-password users must use "Forgot Password" on first login. Federated
  (OIDC/SAML) users are unaffected.
- **"Resource already exists" during the platform deploy** means a retained
  resource wasn't cleaned up. Find it in the CloudFormation error event, delete
  it, and re-run.
- **"Table not found" during restore** means the platform deploy didn't finish
  or a table name changed. Confirm `npx cdk list` shows the stack and that the
  SSM parameters under your prefix are published.
