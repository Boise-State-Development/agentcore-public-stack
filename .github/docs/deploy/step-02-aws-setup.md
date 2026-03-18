# Step 2 of 5 — AWS Setup

✅ Step 1: Prerequisites<br>
➡️ **Step 2: AWS Setup** ← You are here<br>
⬜ Step 3: Configure GitHub<br>
⬜ Step 4: Deploy Workflows<br>
⬜ Step 5: Verify Deployment

⏱️ ~15 minutes · 🟡 Moderate · Requires: AWS Console access

---

Before configuring GitHub, you need three things set up in AWS: authentication credentials, a Route 53 hosted zone, and two ACM certificates.

## What you'll need for this step

- AWS Console access with admin permissions
- Your domain name (e.g. `example.com`)
- A pen/notepad to record values you'll need in Step 3

---

## 2a. Set Up AWS Authentication

Choose **one** method. GitHub Actions will use these credentials to deploy resources.

### Option 1: OIDC Role (recommended)

No long-lived keys to rotate. GitHub Actions assumes an IAM role via OpenID Connect.

1. Create an OIDC identity provider in IAM for GitHub Actions
2. Create an IAM role that trusts the GitHub OIDC provider
3. Attach sufficient permissions to the role (see Prerequisites for scope)

**References:**
- [GitHub Docs: Configuring OIDC in AWS](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [AWS Docs: Create an OIDC identity provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html)

> [!TIP]
> OIDC is the recommended approach. It eliminates the need to rotate access keys and follows AWS security best practices.

**Note down:** `IAM Role ARN` (e.g. `arn:aws:iam::123456789012:role/github-actions-deploy`)

### Option 2: IAM Access Keys (simpler, less secure)

Create an IAM user with programmatic access and generate an access key pair.

> [!WARNING]
> Access keys are long-lived credentials. If you use this method, ensure you rotate them regularly and never commit them to source control.

**Note down:** `Access Key ID` and `Secret Access Key`

<details>
<summary>How do I choose between OIDC and access keys?</summary>

**Use OIDC if:** You're deploying to a production or shared account, your organization has security requirements, or you want a set-and-forget setup.

**Use access keys if:** You're doing a quick proof-of-concept, working in a sandbox account, or need the simplest possible setup.

Both methods work identically for deployment — the only difference is how GitHub Actions authenticates with AWS.

</details>

---

## 2b. Create a Route 53 Hosted Zone

Create a **public hosted zone** for your domain (e.g. `example.com`).

1. Open the [Route 53 Console](https://console.aws.amazon.com/route53/)
2. Go to **Hosted zones** → **Create hosted zone**
3. Enter your domain name and select **Public hosted zone**
4. If your domain is registered outside AWS, update your registrar's nameservers to point to the Route 53 NS records

**Reference:** [Creating a public hosted zone](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/CreatingHostedZone.html)

> [!NOTE]
> This step only needs to be done once per domain. If you already have a Route 53 hosted zone for your domain, skip this.

**Note down:** `Hosted zone domain` (e.g. `example.com`)

<details>
<summary>How do I verify my hosted zone is working?</summary>

After creating the hosted zone and updating nameservers, run:

```bash
dig NS example.com
```

You should see the four Route 53 nameservers in the response. DNS propagation can take up to 48 hours, but usually completes within minutes.

</details>

---

## 2c. Create ACM Certificates

You need **two** TLS certificates. Each should cover both your apex domain and wildcard subdomains.

When requesting each certificate in ACM, add **two domain names**:
- `example.com`
- `*.example.com`

This allows the certificate to cover subdomains like `api.example.com` and `app.example.com`.

| Certificate | Region | Used By |
|-------------|--------|---------|
| **ALB certificate** | Your deployment region (e.g. `us-west-2`) | Application Load Balancer (`api.example.com`) |
| **CloudFront certificate** | `us-east-1` (required) | Frontend CDN (`app.example.com`) |

> [!IMPORTANT]
> The CloudFront certificate **must** be in `us-east-1`, regardless of your deployment region. This is an AWS requirement.

**How to create each certificate:**

1. Open the [ACM Console](https://console.aws.amazon.com/acm/) — make sure you're in the correct region
2. Click **Request a certificate** → **Request a public certificate**
3. Enter `example.com` as the domain name
4. Click **Add another name** and enter `*.example.com`
5. Choose **DNS validation** (recommended)
6. If your domain is in Route 53, ACM can create the validation records automatically
7. Wait for the status to change to **Issued**

**Repeat this process twice** — once in your deployment region, once in `us-east-1`.

**Reference:** [Requesting a public certificate](https://docs.aws.amazon.com/acm/latest/userguide/gs-acm-request-public.html)

> [!WARNING]
> Do not proceed to the next step until both certificates show **Issued** status. Validation can take a few minutes.

**Note down:**
- `ALB Certificate ARN` (e.g. `arn:aws:acm:us-west-2:123456789012:certificate/abc-123`)
- `CloudFront Certificate ARN` (e.g. `arn:aws:acm:us-east-1:123456789012:certificate/def-456`)

<details>
<summary>My certificate is stuck in "Pending validation"</summary>

This usually means the DNS validation records weren't created. Check that:
1. The CNAME records from ACM exist in your Route 53 hosted zone (or external DNS)
2. Your domain's nameservers are correctly pointing to Route 53
3. You waited at least 5 minutes — validation can take a moment

If using Route 53, ACM offers a **"Create records in Route 53"** button that handles this automatically.

</details>

---

## 2d. Enable X-Ray Transaction Search (once per account)

X-Ray Transaction Search is an **account-level singleton** — it cannot be managed via CloudFormation if it already exists. You must configure it via the AWS CLI.

> [!IMPORTANT]
> Before running the commands below, replace these placeholders with your actual values:
> - `PARTITION` — usually `aws` (or `aws-cn`, `aws-us-gov`)
> - `REGION` — your deployment region (e.g. `us-west-2`)
> - `ACCOUNT_ID` — your 12-digit AWS account ID

**1. Create the CloudWatch Logs resource policy for X-Ray:**

```bash
aws logs put-resource-policy \
  --policy-name XRayTransactionSearchPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "TransactionSearchXRayAccess",
        "Effect": "Allow",
        "Principal": { "Service": "xray.amazonaws.com" },
        "Action": "logs:PutLogEvents",
        "Resource": [
          "arn:PARTITION:logs:REGION:ACCOUNT_ID:log-group:aws/spans:*",
          "arn:PARTITION:logs:REGION:ACCOUNT_ID:log-group:/aws/application-signals/data:*"
        ],
        "Condition": {
          "ArnLike": {
            "aws:SourceArn": "arn:PARTITION:xray:REGION:ACCOUNT_ID:*"
          },
          "StringEquals": {
            "aws:SourceAccount": "ACCOUNT_ID"
          }
        }
      }
    ]
  }'
```

**2. Set the trace segment destination to CloudWatch Logs:**

```bash
aws xray update-trace-segment-destination --destination CloudWatchLogs
```

**3. Configure the indexing sampling percentage:**

```bash
aws xray update-indexing-rule \
  --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 5}}'
```

> [!TIP]
> A sampling percentage of `5` is a reasonable starting point for most workloads. Increase to `100` if you need full trace visibility during debugging.

**Reference:** [Enable Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html#CloudWatch-Transaction-Search-EnableAPI)

> [!NOTE]
> This only needs to be done once per AWS account. If Transaction Search is already enabled in your account, skip this step.

<details>
<summary>How do I verify Transaction Search is enabled?</summary>

Run:

```bash
aws xray get-trace-segment-destination
```

If configured correctly, the output should show `CloudWatchLogs` as the destination.

</details>


## Values to Carry Forward

Before moving to Step 3, confirm you have these values recorded:

| Value | Example | From |
|-------|---------|------|
| AWS auth method | OIDC or access keys | Step 2a |
| IAM Role ARN _or_ Access Key pair | `arn:aws:iam::...` | Step 2a |
| Hosted zone domain | `example.com` | Step 2b |
| ALB Certificate ARN | `arn:aws:acm:us-west-2:...` | Step 2c |
| CloudFront Certificate ARN | `arn:aws:acm:us-east-1:...` | Step 2c |

---

### ➡️ [Next: Step 3 — Configure GitHub](./step-03-github-config.md)
