# Step 3 of 5 — Configure GitHub

✅ Step 1: Prerequisites<br>
✅ Step 2: AWS Setup<br>
➡️ **Step 3: Configure GitHub** ← You are here<br>
⬜ Step 4: Deploy Workflows<br>
⬜ Step 5: Verify Deployment

⏱️ ~10 minutes · 🟡 Moderate · Requires: GitHub repo admin access

---

In this step you'll add all the configuration values from Step 2 (plus a few new ones) to your forked GitHub repository. The deployment workflows read these values at runtime.

## What you'll need for this step

- Admin access to your forked repository on GitHub
- The values you noted in Step 2 (role ARN or access keys, domain, certificate ARNs)
- Your AWS account ID (12-digit number)
- (Optional) Your identity provider credentials if you plan to add federated login later

---

## Where to add these values

Go to your forked repository on GitHub:

**Settings → Secrets and variables → Actions**

You'll add values in two places:
- **Secrets** tab — encrypted values that never appear in logs
- **Variables** tab — non-sensitive configuration values

---

## 3a. AWS Credentials (Secrets)

Add your AWS authentication credentials as **repository secrets**.

> [!WARNING]
> Never commit AWS credentials to your repository. Always use GitHub Secrets for sensitive values.

**If using OIDC (recommended):**

| Secret Name | Value |
|-------------|-------|
| `AWS_ROLE_ARN` | IAM role ARN from Step 2a |

**If using access keys:**

| Secret Name | Value |
|-------------|-------|
| `AWS_ACCESS_KEY_ID` | Access key ID from Step 2a |
| `AWS_SECRET_ACCESS_KEY` | Secret access key from Step 2a |

---

## 3b. Deployment Variables

Switch to the **Variables** tab and add these values. All are required.

| Variable Name | Example | Description |
|---------------|---------|-------------|
| `AWS_REGION` | `us-west-2` | AWS region for all resources |
| `CDK_AWS_ACCOUNT` | `123456789012` | Your 12-digit AWS account ID |
| `CDK_PROJECT_PREFIX` | `agentcore` | Unique prefix for all AWS resource names |
| `CDK_HOSTED_ZONE_DOMAIN` | `example.com` | Route 53 hosted zone domain (from Step 2b) |
| `CDK_ALB_SUBDOMAIN` | `api` | Subdomain for the API load balancer |
| `CDK_DOMAIN_NAME` | `app.example.com` | Full domain for the frontend |
| `CDK_CERTIFICATE_ARN` | `arn:aws:acm:us-west-2:...` | ALB certificate ARN (from Step 2c). **Your deployment region.** |
| `CDK_CLOUDFRONT_CERTIFICATE_ARN` | `arn:aws:acm:us-east-1:...` | Shared CloudFront certificate ARN (from Step 2c). **Must be in `us-east-1`** and cover `{CDK_DOMAIN_NAME}` + `*.{CDK_DOMAIN_NAME}`. Serves the SPA, artifacts, and MCP-sandbox origins. Required for a domained deploy — a missing cert fails at `cdk synth`. |

<details>
<summary>How do I find my AWS account ID?</summary>

1. Open the [AWS Console](https://console.aws.amazon.com/)
2. Click your account name in the top-right corner
3. Your 12-digit account ID is displayed in the dropdown

Or run this in your terminal:

```bash
aws sts get-caller-identity --query Account --output text
```

</details>

<details>
<summary>What should I use for CDK_PROJECT_PREFIX?</summary>

This prefix is prepended to all AWS resource names to avoid conflicts. Use something short and unique to your project or organization — for example `myco-ai` or `agentcore`. Only lowercase letters, numbers, and hyphens.

</details>

> [!TIP]
> These are the minimum required variables. For optional settings like ECS sizing, CloudFront price class, CORS origins, and more, see the [Full Configuration Reference](../../ACTIONS-REFERENCE.md).

### Feature & Edge Configuration

Artifacts, the MCP Apps sandbox, and SageMaker fine-tuning are **always provisioned** as part of `PlatformStack` — there are no `CDK_*_ENABLED` flags. When `CDK_DOMAIN_NAME` is set, all three CloudFront origins (SPA, artifacts, MCP-sandbox) need a `us-east-1` certificate. The simplest correct setup is the single shared **`CDK_CLOUDFRONT_CERTIFICATE_ARN`** in the required table above — every origin falls back to it. **A domained deploy with no effective cert for any origin fails at `cdk synth`** (it aborts before shipping an origin with no Route 53 record, rather than silently degrading to the CloudFront default domain).

The per-origin cert vars below are **optional overrides** — set one only if you deliberately want that origin to use a *different* certificate than the shared one. If `CDK_CLOUDFRONT_CERTIFICATE_ARN` is set, you can leave all three blank.

| Variable Name | Default | Description |
|---------------|---------|-------------|
| `CDK_FRONTEND_CERTIFICATE_ARN` | falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | Override cert for the SPA origin (`{CDK_DOMAIN_NAME}`). **Must be in `us-east-1`.** |
| `CDK_ARTIFACTS_CERTIFICATE_ARN` | falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | Override cert for the artifacts origin (`artifacts.{CDK_DOMAIN_NAME}`). **Must be in `us-east-1`.** Same wildcard-depth rule as the shared cert — see [Step 2c](./step-02-aws-setup.md#2c-create-acm-certificates). |
| `CDK_MCP_SANDBOX_CERTIFICATE_ARN` | falls back to `CDK_CLOUDFRONT_CERTIFICATE_ARN` | Override cert for the MCP Apps sandbox origin (`mcp-sandbox.{CDK_DOMAIN_NAME}`) — the cross-origin shell the SPA frames MCP Apps in. **Must be in `us-east-1`.** Without an effective cert (neither this nor the shared var) a domained deploy fails at synth, because otherwise the sandbox would land on the CloudFront default domain with no Route 53 ALIAS and every MCP App would fail to load with a `chrome-error` postMessage error. See [Step 2c](./step-02-aws-setup.md#2c-create-acm-certificates). |
| `CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS` | — | Comma-separated extra origins (beyond `https://{CDK_DOMAIN_NAME}`) allowed to embed artifact iframes via CSP `frame-ancestors` — applied to both the CloudFront response-headers policy and the render Lambda. Set to `http://localhost:4200` to point a local SPA at this deployment. **Leave unset in production**: every listed origin can frame your users' artifacts (still render-token gated, but a real loosening on a shared environment). |
| `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS` | — | Comma-separated extra origins (beyond `https://{CDK_DOMAIN_NAME}`) allowed to embed the MCP Apps sandbox proxy via CSP `frame-ancestors`. Set to `http://localhost:4200` to point a local SPA at this deployment. **Leave unset in production.** |
| `CDK_FINE_TUNING_CORS_ORIGINS` | — | Comma-separated extra CORS origins for the SageMaker fine-tuning data bucket, beyond `https://{CDK_DOMAIN_NAME}`. Optional — fine-tuning itself is always provisioned. |

---

## 3c. Authentication

Authentication is handled automatically by Amazon Cognito, which is deployed as part of the infrastructure stack. No identity provider configuration is needed before deployment.

After deployment, the first person to access the application will complete a first-boot setup to create the initial admin account with username, email, and password. The admin can then add federated identity providers (Entra ID, Okta, Google, etc.) through the admin dashboard.

<details>
<summary>Quick reference: what values did I note in Step 2?</summary>

| Value | Where to enter it |
|-------|-------------------|
| IAM Role ARN | `AWS_ROLE_ARN` secret |
| _or_ Access Key ID | `AWS_ACCESS_KEY_ID` secret |
| _or_ Secret Access Key | `AWS_SECRET_ACCESS_KEY` secret |
| Hosted zone domain | `CDK_HOSTED_ZONE_DOMAIN` variable |
| ALB Certificate ARN | `CDK_CERTIFICATE_ARN` variable |
| CloudFront Certificate ARN | `CDK_FRONTEND_CERTIFICATE_ARN` variable |

</details>

---

## Verification Checklist

Before proceeding, confirm:

- [ ] AWS credentials are saved as secrets (either `AWS_ROLE_ARN` or the access key pair)
- [ ] All 8 required variables from section 3b are set

---

### ➡️ [Next: Step 4 — Deploy Workflows](./step-04-deploy.md)
