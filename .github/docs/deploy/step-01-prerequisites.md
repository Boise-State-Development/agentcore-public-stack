# Step 1 of 5 — Prerequisites

➡️ **Step 1: Prerequisites** ← You are here<br>
⬜ Step 2: AWS Setup<br>
⬜ Step 3: Configure GitHub<br>
⬜ Step 4: Deploy Workflows<br>
⬜ Step 5: Verify Deployment

⏱️ ~5 minutes · 🟢 Easy

---

## What You'll Need

Confirm you have access to the following before proceeding. Everything on this list is required.

### Accounts & Access

- [ ] **AWS account** with administrator access (or permissions to create IAM roles, VPCs, ECS clusters, CloudFront distributions, Route 53 zones, ACM certificates, Lambda functions, and DynamoDB tables)
- [ ] **GitHub account** with a fork of this repository
- [ ] **Domain name** you control (e.g. `example.com`) with the ability to update nameservers

### Identity Provider (Optional)

Authentication is handled automatically via Amazon Cognito, which is deployed as part of the infrastructure stack. On first access, you'll create an admin account directly — no external identity provider is needed to get started.

If you want federated login (e.g., corporate SSO), you can optionally configure an external OIDC provider later through the admin UI:

- **Microsoft Entra ID** (Azure AD)
- **Okta**
- **Google Workspace**
- **Any OIDC-compliant provider**

> [!NOTE]
> No identity provider setup is required before deployment. Cognito handles initial authentication, and federated providers can be added post-deployment through the admin dashboard.

<details>
<summary>What if I don't have a domain yet?</summary>

You can register a domain through [AWS Route 53](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/domain-register.html) or any domain registrar. If you use Route 53, the hosted zone is created automatically. If you use an external registrar, you'll need to point the domain's nameservers to the Route 53 hosted zone you create in Step 2.

</details>

<details>
<summary>What if I want to add a federated identity provider later?</summary>

After deployment, the first-boot flow creates your admin account using Cognito. Once logged in as admin, you can add federated identity providers (Entra ID, Okta, Google, etc.) through the admin dashboard. The system registers them in Cognito automatically — no redeployment needed.

</details>

<details>
<summary>What AWS permissions do I need exactly?</summary>

The deployment creates resources across many AWS services. The simplest approach is to use an account with `AdministratorAccess`. If you need a least-privilege policy, the deployment touches: IAM, VPC, ECS, ECR, ALB, Route 53, ACM, CloudFront, S3, DynamoDB, Lambda, API Gateway, CloudWatch, and Secrets Manager.

</details>

---

## Ready?

Once you have everything checked off above, move on to setting up your AWS resources.

---

### ➡️ [Next: Step 2 — AWS Setup](./step-02-aws-setup.md)
