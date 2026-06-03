# Upgrading from the Multi-Stack Architecture

This guide walks you through migrating from the previous multi-stack architecture (9 separate CloudFormation stacks) to the current single-stack architecture (`PlatformStack`). The process involves a full backup, teardown, redeploy, and restore.

> ⚠️ **This is a destructive migration.** The old stacks are deleted and replaced with a single new stack. Data is preserved via the backup/restore workflow, but the process requires careful execution in the correct order.

---

## Prerequisites

Before starting:

- [ ] You have admin access to the GitHub repository (to trigger workflows)
- [ ] You have the latest code on `main` (or the branch with the new single-stack architecture)
- [ ] Your GitHub environment variables and secrets are configured (see [step-03-github-config.md](step-03-github-config.md))
- [ ] You know your `CDK_PROJECT_PREFIX` (e.g., `boisestate-prod`)
- [ ] You know which GitHub environment you're migrating (`production`, `development`, etc.)

---

## Migration Steps

### Step 1: Pull the Latest Code

Ensure your repository has the new single-stack architecture. If you're on a fork, sync with upstream first.

```bash
git checkout main
git pull origin main
```

Verify the new architecture is in place:
```bash
ls infrastructure/lib/platform-stack.ts   # should exist
ls infrastructure/bin/infrastructure.ts    # should show only PlatformStack
```

---

### Step 2: Run the Backup Workflow

**This is the most critical step.** The backup captures all application data so it can be restored after the infrastructure is rebuilt.

1. Go to **Actions** → **Backup Data (Pre-Migration)**
2. Click **Run workflow** with these inputs:

| Input | Value |
|-------|-------|
| `project_prefix` | Your `CDK_PROJECT_PREFIX` (e.g., `boisestate-prod`) |
| `aws_region` | Your AWS region (e.g., `us-west-2`) |
| `aws_environment` | The GitHub environment to use (e.g., `production`) |
| `include_ephemeral` | `false` (session tables aren't worth preserving) |

3. Wait for the workflow to complete successfully.
4. **Note the backup bucket name** from the workflow output — it will be something like `{prefix}-backup-{timestamp}` (e.g., `boisestate-prod-backup-20260527T183042Z`).
5. **Verify the backup** by checking the workflow logs for `summary.ok` count and zero `summary.failed`.

> ⚠️ **Do NOT proceed if the backup workflow fails or reports any `failed` components.** Fix the issue and re-run the backup first.

---

### Step 3: Run the Teardown Workflow

This destroys all existing CloudFormation stacks in the target environment.

1. Go to **Actions** → **Teardown All Infrastructure**
2. Click **Run workflow** with these inputs:

| Input | Value |
|-------|-------|
| `environment` | The environment to tear down (e.g., `production`) |
| `confirm` | Type `DESTROY` (exactly, all caps) |

3. Wait for the workflow to complete.

---

### Step 4: Clean Up Retained Resources

If your environment had `CDK_RETAIN_DATA_ON_DELETE=true` (the default for production), CloudFormation will have **retained** certain resources instead of deleting them. These must be manually deleted before the new stack can create replacements with the same names.

**Resources that are typically retained:**

- **DynamoDB tables** — all ~24 application tables
- **S3 buckets** — file uploads, RAG documents, artifacts content, fine-tuning data
- **Cognito User Pool** — the identity store
- **Secrets Manager secrets** — auth secret, OAuth secrets, BFF cookie key
- **KMS keys** — OAuth token encryption, BFF cookie signing
- **SSM parameters under `/${CDK_PROJECT_PREFIX}/`** — these were written by the
  pre-multi-stack scripts via direct `aws ssm put-parameter` calls (not
  CloudFormation), so `delete-stack` doesn't see them. Most importantly,
  `/${CDK_PROJECT_PREFIX}/{app-api,inference-api}/image-tag` will hold a
  legacy tag-only value (e.g. a git short SHA) from your last pre-migration
  deploy. The new architecture treats these parameters as **full ECR URIs**
  and the first PlatformStack deploy will fail CFN early-validation if the
  legacy value is still present:
  ```
  Property value [<short-sha>] does not match pattern:
    ^\d{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/...
  ```
  The seed step in `scripts/platform/deploy.sh` repairs this automatically
  on the next platform deploy (it overwrites any non-URI value with the
  bootstrap container URI), but you can also clean it up manually below.

**How to identify retained resources:**

```bash
# List DynamoDB tables with your prefix
aws dynamodb list-tables --query "TableNames[?starts_with(@, '${CDK_PROJECT_PREFIX}')]"

# List S3 buckets with your prefix
aws s3 ls | grep "${CDK_PROJECT_PREFIX}"

# List Cognito user pools
aws cognito-idp list-user-pools --max-results 20 --query "UserPools[?starts_with(Name, '${CDK_PROJECT_PREFIX}')]"

# List secrets
aws secretsmanager list-secrets --query "SecretList[?starts_with(Name, '${CDK_PROJECT_PREFIX}')].[Name]"

# List SSM parameters under your project prefix
aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n'
```

**Delete them:**

```bash
# DynamoDB tables (repeat for each table)
aws dynamodb delete-table --table-name {prefix}-users
aws dynamodb delete-table --table-name {prefix}-app-roles
# ... etc for all ~24 tables

# S3 buckets (must empty first)
aws s3 rb s3://{prefix}-user-file-uploads --force
aws s3 rb s3://{prefix}-rag-documents --force
# ... etc

# Cognito user pool
aws cognito-idp delete-user-pool --user-pool-id {pool-id}

# Secrets (force delete without recovery window)
aws secretsmanager delete-secret --secret-id {prefix}-auth-secret --force-delete-without-recovery
# ... etc

# SSM parameters under your project prefix.
# Minimum required for an in-place migration is just the image-tag params,
# which break the first PlatformStack deploy if left at a legacy tag-only
# value. You can either delete just those two:
aws ssm delete-parameter --name "/${CDK_PROJECT_PREFIX}/app-api/image-tag"        2>/dev/null || true
aws ssm delete-parameter --name "/${CDK_PROJECT_PREFIX}/inference-api/image-tag"  2>/dev/null || true

# ...or sweep every SSM parameter under your prefix (safe — they're all
# re-published on the next platform/backend deploy):
aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive \
  --query 'Parameters[].Name' --output text \
  | tr '\t' '\n' \
  | xargs -r -n10 aws ssm delete-parameters --names
```

> 💡 **Tip:** If you set `CDK_RETAIN_DATA_ON_DELETE=false` in your GitHub environment variables BEFORE running teardown, CloudFormation will delete everything automatically and you can skip this step entirely. Only do this if you're confident your backup is good.

> ⚠️ **Do NOT delete the backup bucket** (`{prefix}-backup-{timestamp}`). You need it for the restore step.

---

### Step 5: Deploy the New Architecture

With the old stacks gone and retained resources cleaned up, deploy the new single-stack architecture.

1. Go to **Actions** → **Platform Stack**
2. Click **Run workflow** (or push to `main`/`develop` to trigger automatically)
3. Wait for the CDK deploy to complete (~15 minutes for a fresh deploy)

This provisions all infrastructure in a single `PlatformStack` — VPC, ALB, DynamoDB tables, S3 buckets, Cognito, CloudFront, AgentCore, ECS, Lambdas, everything.

4. After Platform succeeds, run **Backend Deploy** to build and deploy application code:
   - Go to **Actions** → **Backend Deploy** → **Run workflow**
   - This builds Docker images, pushes to ECR, and updates ECS/Lambda/Runtime

5. Run **Frontend Deploy** to deploy the Angular SPA:
   - Go to **Actions** → **Frontend Deploy** → **Run workflow**

6. Run **Bootstrap Data Seeding** to seed default configuration:
   - Go to **Actions** → **Bootstrap Data Seeding** → **Run workflow**

---

### Step 6: Run the Restore Workflow

Now restore your backed-up data into the freshly deployed infrastructure.

1. Go to **Actions** → **Restore Data**
2. Click **Run workflow** with these inputs:

| Input | Value |
|-------|-------|
| `backup_bucket` | The bucket name from Step 2 (e.g., `boisestate-prod-backup-20260527T183042Z`) |
| `manifest_key` | `{prefix}/{timestamp}/manifest.json` (e.g., `boisestate-prod/20260527T183042Z/manifest.json`) |
| `target_prefix` | Your `CDK_PROJECT_PREFIX` (same as before) |
| `region` | Your AWS region |
| `dry_run` | `true` first (to verify), then `false` to execute |
| `skip_cognito_users` | `false` (unless you want users to re-register) |

3. **Run with `dry_run=true` first** — review the output to confirm it found all your data.
4. **Run again with `dry_run=false`** — this writes the data into the new tables and buckets.

---

### Step 7: Verify

1. Visit your application URL and confirm:
   - [ ] Login works (Cognito users restored)
   - [ ] Chat history is present (DynamoDB sessions restored)
   - [ ] File uploads are accessible (S3 data restored)
   - [ ] Admin dashboard shows users, costs, models (DynamoDB data restored)
   - [ ] RAG assistants work (documents + vectors restored)

2. Check the GitHub Actions dashboard — all workflows should show green.

---

## Troubleshooting

### "Resource already exists" during Platform deploy

A retained resource from the old stacks wasn't cleaned up. Check the CloudFormation error event for the resource name, delete it manually, and re-run the deploy.

### "Table not found" during Restore

The Platform deploy didn't complete successfully, or the table name changed. Verify `npx cdk list` shows the stack, and check SSM parameters are published:

```bash
aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive --query "Parameters[].Name"
```

### Cognito users can't log in after restore

Cognito password hashes are not exportable by AWS. Native-password users will need to use "Forgot Password" on first login. Federated (OIDC/SAML) users are unaffected — their identity provider handles authentication.

### Backup bucket is missing

The backup bucket is created with a timestamp suffix and is NOT deleted by the teardown workflow. Check:

```bash
aws s3 ls | grep "${CDK_PROJECT_PREFIX}-backup"
```

---

## Rollback

If the migration fails and you need to go back:

1. Run the **Teardown** workflow to destroy the new stack
2. Switch your repository back to the old multi-stack branch
3. Re-deploy the old architecture via its workflows
4. Restore data from the same backup bucket

The backup bucket is immutable and survives all teardown operations.

---

## Timeline Estimate

| Step | Duration | Notes |
|------|----------|-------|
| Backup | 5–15 min | Depends on data volume |
| Teardown | 5–10 min | Parallel stack deletion |
| Cleanup retained resources | 5–15 min | Manual; skip if `retainDataOnDelete=false` |
| Platform deploy | 10–15 min | Fresh CDK deploy |
| Backend deploy | 3–5 min | Docker builds + AWS API updates |
| Frontend deploy | 2 min | S3 sync + invalidation |
| Bootstrap seeding | 1 min | Default config data |
| Restore | 5–15 min | Depends on data volume |
| **Total** | **~45–75 min** | |
