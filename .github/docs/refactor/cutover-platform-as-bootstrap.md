# Cutover: from the 2-stack architecture to the unified PlatformStack

**Audience:** anyone deploying this fork's `main` branch for the first time after the platform-as-bootstrap refactor (commits `c1cc4cf5` through Phase 7's collapse) lands.

**TL;DR:** the refactor changes which CloudFormation stack owns most resources. Going from "two stacks with stuff in both" to "one stack with everything" is not a CFN-native operation — you have to tear down the old layout first, then deploy the new one fresh. This doc tells you how.

---

## 1. Why is a cutover needed?

CDK's `cdk deploy` will not relocate a resource from stack A to stack B. To CFN, that's "delete from A" + "create new in B" — and CFN does the steps in opposite stacks, with no awareness of the relationship. If you `cdk deploy` the new architecture against an environment that already has the old layout deployed, you'll end up with:

* Resources that exist twice (old in BackendStack, new in PlatformStack) — CFN rejects most second-creates because of unique-name collisions (DynamoDB tables, log groups, etc.).
* Resources that exist nowhere (old's BackendStack tries to delete on a logical-ID change but the new template doesn't have them at the same logical ID anymore).
* Half-deleted Memory / AgentCore resources stuck in transitional states.

The clean path is: **delete everything with the project prefix, then deploy fresh.**

---

## 2. Pre-cutover checklist

Run through this BEFORE deleting anything.

* [ ] **Take backups of any data you care about.** The unified stack's `RemovalPolicy` matches what was set in the old layout (DESTROY in non-prod, RETAIN in prod), but cutover is a manual delete-and-recreate, so retention policy doesn't help you. Specifically:
    * DynamoDB tables — `aws dynamodb create-backup` for each table named `${prefix}-*`.
    * S3 buckets — sync to a backup bucket: `aws s3 sync s3://${prefix}-* s3://my-backup-bucket/${prefix}-*/`.
    * Cognito user pool — `aws cognito-idp list-users --user-pool-id ...` and stash the output.
    * AgentCore Memory contents — these can't be exported via API today; if you've collected meaningful conversation memory, it's lost.
* [ ] **Pause any running deployments.** Especially nightly. Check `gh run list` or the GitHub Actions UI.
* [ ] **Confirm the old environment is in a STABLE state**, not mid-deploy. Run `aws cloudformation describe-stacks --query 'Stacks[?starts_with(StackName, \`${prefix}\`)]'` and verify every stack's `StackStatus` is one of `*_COMPLETE` (not `*_IN_PROGRESS` or `*_FAILED`). If anything is `IN_PROGRESS`, wait for it to settle.
* [ ] **Check ECR repositories** for images you want to keep. The teardown does NOT delete ECR repos; they're managed separately. Existing image tags will still be there after cutover.

---

## 3. Cutover procedure

### Step 1 — teardown the old stacks

The teardown script handles both legacy 2-stack and 9-stack layouts. It uses `aws cloudformation delete-stack` (not `cdk destroy`) so it doesn't depend on the current CDK app's understanding of which stacks exist.

```bash
# From the repo root:
bash scripts/teardown/destroy.sh
```

Or trigger the manual workflow:

```bash
gh workflow run teardown.yml
```

This deletes every stack whose name starts with `${CDK_PROJECT_PREFIX}-`. Watch the AWS console — Memory deletion can take 5-15 minutes.

### Step 2 — confirm clean slate

```bash
aws cloudformation describe-stacks \
    --query "Stacks[?starts_with(StackName, '${CDK_PROJECT_PREFIX}')].StackName" \
    --output text
```

Should print nothing. If any stacks remain in `DELETE_FAILED`, check the console for the failing resource — usually a non-empty S3 bucket or an IAM role with attached external policies. Empty/detach as needed and retry the delete.

### Step 3 — clean up orphaned resources

Some resources don't get deleted by CFN deletes:

* **CloudWatch log groups** auto-created by Lambda invocation (named `/aws/lambda/${prefix}-*`). The unified stack's Lambdas use CDK-auto-generated names so this is mostly harmless, but the orphaned log groups will accumulate. Delete:

    ```bash
    aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${CDK_PROJECT_PREFIX}-" \
        --query 'logGroups[*].logGroupName' --output text \
        | xargs -n1 aws logs delete-log-group --log-group-name
    ```

* **AgentCore vended-logs log groups** at `/aws/vendedlogs/bedrock-agentcore/...`. Same pattern as above.

* **SSM parameters** under `/${prefix}/*`. The teardown script doesn't touch these (it's a delete-stack, not a delete-resource scan). Most are recreated by the new deploy and will overwrite cleanly, but if you want a truly empty slate:

    ```bash
    aws ssm get-parameters-by-path --path "/${CDK_PROJECT_PREFIX}/" --recursive \
        --query 'Parameters[*].Name' --output text \
        | xargs -n1 aws ssm delete-parameter --name
    ```

* **AgentCore Memory** in `DELETING` state can take a long time. Check status:

    ```bash
    aws bedrock-agentcore-control list-memories \
        --query "memorySummaries[?starts_with(name, '${CDK_PROJECT_PREFIX}')].{Name:name,Status:status}"
    ```

    Wait for the Memory to disappear before proceeding.

### Step 4 — first deploy of the unified stack

```bash
gh workflow run platform.yml
```

Or locally:

```bash
bash scripts/platform/deploy.sh
```

The first deploy creates **everything** — VPC, ALB, all the data layer, AgentCore resources (Memory takes 5-15 minutes), all the compute. Expect this to take 25-30 minutes end-to-end. This is once-ever.

### Step 5 — first code deploys

After the unified stack is `CREATE_COMPLETE`, run the backend workflow to ship the real Lambda code (CDK initially seeded the artifact-render and rag-ingestion Lambdas with bootstrap stubs):

```bash
gh workflow run backend.yml
```

Verify:

* `aws lambda get-function-configuration --function-name $(aws ssm get-parameter --name "/${CDK_PROJECT_PREFIX}/artifacts/render-function-name" --query 'Parameter.Value' --output text)` — `Code.ImageUri` should point at your real ECR repo, not the cdk-assets bootstrap.
* The artifacts subdomain serves real artifact HTML, not the 503 bootstrap stub.

### Step 6 — first frontend deploy

```bash
gh workflow run frontend-deploy.yml
```

---

## 4. Rollback

If the new architecture deploy fails partway, you have two options:

**Option A: Forward-fix.** The unified stack's `CREATE_FAILED` status is recoverable in most cases. Read the CFN console's reason for failure, fix the underlying issue (often an IAM permission or quota), and re-run `cdk deploy`. CFN will resume from where it left off.

**Option B: Roll back to the old architecture.** This means reverting `main` to the commit before `c1cc4cf5` (the start of the platform-as-bootstrap refactor) and running the old 2-stack deploy. You'd also need to teardown the partial unified stack first via the teardown workflow.

Forward-fix is almost always better. The bootstrap pattern means most code-related issues can be resolved by re-running the appropriate workflow job.

---

## 5. Known gotchas

* **First Platform deploy creates AgentCore Memory**, which is non-idempotent during the 5-15 minute `CREATING` window. If the deploy fails for ANY reason during Memory creation, the next attempt will fail with `Validation failed during DeleteMemory: Memory is in transitional state CREATING`. Wait for Memory to reach `READY` (visible in the AgentCore console) before retrying.
* **Bootstrap-stub period**: between Step 4 and Step 5, the artifact-render Lambda returns a 503 placeholder. The artifacts subdomain shows "Artifact service is updating, please retry in a moment." If anyone tries to use the platform during this window, they'll see this. Run Step 5 promptly after Step 4 finishes.
* **rag-ingestion bootstrap**: the bootstrap container logs and ignores S3 events. If a document upload happens during the bootstrap window, ingestion is silently skipped — the user will need to re-upload the document after the real handler is deployed.
* **CloudFront propagation**: distributions take ~10 minutes to fully propagate after creation. The `cdk deploy` returns long before that. If the SPA or artifacts subdomains return DNS errors immediately after deploy, give it 10-15 minutes.
* **Cognito first-boot**: the first user to access the application becomes the admin (this is the project's existing first-boot behaviour, not changed by the refactor). Make sure the right person logs in first.

---

## 6. Phases 5 + 6 (now landed)

The original cutover doc described Phases 5 + 6 as future work. Both have since landed:

* **Phase 5 (AgentCore Runtime)**: CDK now ships a stable bootstrap container (`infrastructure/bootstrap-assets/inference-api/`, stdlib HTTP server on port 8080 with `/ping` health check). The backend workflow's `deploy-inference-api-code` job calls `aws bedrock-agentcore-control update-agent-runtime` with the project's freshly-built ECR image URI. Polls for the runtime to be in `READY` state before and after.
* **Phase 6 (App API ECS Fargate)**: CDK ships a bootstrap task def with a stable container (`infrastructure/bootstrap-assets/app-api/`, stdlib HTTP server on port 8000 with `/health`). The workflow's `deploy-app-api-code` job does `aws ecs register-task-definition` (mutates the live task def's `containerDefinitions[0].image` field, registers a new revision of the same family), then `aws ecs update-service`, then `aws ecs wait services-stable`.

After Phase 6, the transitional `deploy` job in `backend.yml` (which was running `scripts/platform/deploy.sh` so app-api/inference-api code changes still triggered a CFN deploy) was deleted. Backend code changes now flow exclusively through the four `deploy-*-code` jobs — no `cdk deploy` step in `backend.yml` at all. `platform.yml` is the single CFN entry point.

The bootstrap window is brief but real. During the first deploy of the unified stack, the App API and Inference API serve requests from their bootstrap containers (which return graceful 503 responses) until the workflow's code-deploy step ships the real images. Same-day operational mitigation is the same as for artifact-render and rag-ingestion: run Step 5 of this cutover promptly after Step 4 finishes.
