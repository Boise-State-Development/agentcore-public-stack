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
- Your identity provider credentials (client ID, client secret, issuer URL)

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
| `CDK_CERTIFICATE_ARN` | `arn:aws:acm:us-west-2:...` | ALB certificate ARN (from Step 2c) |
| `CDK_FRONTEND_CERTIFICATE_ARN` | `arn:aws:acm:us-east-1:...` | CloudFront certificate ARN (from Step 2c) |

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

### Optional Features

| Variable Name | Default | Description |
|---------------|---------|-------------|
| `CDK_FINE_TUNING_ENABLED` | `false` | Set to `true` to enable the SageMaker Fine-Tuning stack. Must be set before running the fine-tuning deployment workflow in Step 4. |

---

## 3c. Identity Provider Configuration

These values configure user login for your deployed application. Add them as a mix of **variables** and **secrets**.

### Variables

| Variable Name | Example | Description |
|---------------|---------|-------------|
| `SEED_AUTH_PROVIDER_ID` | `entra-id` | Slug identifier for your IdP |
| `SEED_AUTH_DISPLAY_NAME` | `Microsoft Entra ID` | Display name shown on the login page |
| `SEED_AUTH_ISSUER_URL` | `https://login.microsoftonline.com/TENANT/v2.0` | OIDC issuer URL from your IdP |
| `SEED_AUTH_CLIENT_ID` | `your-client-id` | OAuth client ID from your IdP |

### Secret

| Secret Name | Description |
|-------------|-------------|
| `SEED_AUTH_CLIENT_SECRET` | OAuth client secret from your IdP |

### Optional

| Variable Name | Example | Description |
|---------------|---------|-------------|
| `SEED_ADMIN_JWT_ROLE` | `Admin` | JWT role claim that grants system admin access. Maps to the `system_admin` AppRole via the bootstrap seed script. Must match a role your IdP includes in tokens. |

<details>
<summary>What is SEED_ADMIN_JWT_ROLE and do I need it?</summary>

This is optional but recommended. When set, users whose JWT tokens include this role claim will be granted system admin access in the application. This lets you manage models, tools, roles, and other admin features.

If you skip this, no users will have admin access initially. You can always set it later and re-run the Bootstrap Data Seeding workflow.

</details>

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
- [ ] All 4 identity provider variables from section 3c are set
- [ ] The `SEED_AUTH_CLIENT_SECRET` secret is saved

---

### ➡️ [Next: Step 4 — Deploy Workflows](./step-04-deploy.md)
