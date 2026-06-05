---
title: Troubleshooting
description: Common deployment failures, organized by phase.
sidebar:
  order: 7
---

Most deployment problems trace back to a missing variable, an unvalidated
certificate, or a service that hasn't finished coming up. Find your symptom
below — the sections follow the order you'd hit them in a deploy.

## AWS setup

### Certificate stuck in "Pending validation"

**Cause:** the DNS validation records are missing or haven't propagated.

**Fix:** in the ACM Console, expand the certificate and find its CNAME validation
records. If the domain is in Route 53, click **Create records in Route 53** —
ACM adds them for you. Otherwise add the CNAMEs manually at your DNS provider,
then verify with `dig CNAME _acme-challenge.example.com` and wait a few minutes.

### Hosted zone not resolving

**Symptom:** `dig NS example.com` doesn't return the Route 53 nameservers.

**Cause:** the domain's nameservers haven't been pointed at Route 53.

**Fix:** copy the four NS records from your Route 53 hosted zone into your
registrar's nameserver settings. Propagation usually completes within minutes,
though it can take up to 48 hours.

## GitHub Actions failures

### Authentication or credentials error

**Symptom:** a workflow fails early with an AWS authentication error.

**Fix:**

- **OIDC** — confirm the `AWS_ROLE_ARN` secret is set and the IAM role's trust
  policy allows your fork's OIDC provider.
- **Access keys** — confirm both `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
  secrets are present and correct.
- In both cases, confirm the role or user has sufficient permissions.

### "Variable not found" or empty value

**Cause:** a required GitHub variable is missing — the most common first-deploy
failure.

**Fix:** under **Settings → Secrets and variables → Actions → Variables**,
compare your variables against the
[required list](/agentcore-public-stack/deployment/platform-cdk/#variables), add
any that are missing, and re-run.

### "Resource already exists" on the platform deploy

**Cause:** your `CDK_PROJECT_PREFIX` collides with existing AWS resources —
often leftovers from a previous failed deploy.

**Fix:** choose a more unique prefix, or delete the conflicting resources in AWS
and re-run. (Migrating from the old multi-stack layout? See
[Upgrading from Multi-Stack](/agentcore-public-stack/deployment/upgrade/) — this
is expected there.)

### "No hosted zone found"

**Cause:** `CDK_HOSTED_ZONE_DOMAIN` doesn't match an existing zone, or the zone
lives in a different AWS account.

**Fix:** verify the zone exists in Route 53 in the same account and that the
variable matches the zone name exactly (`example.com`, not `www.example.com`).

## Deployment

### ECS tasks keep restarting

**Symptom:** the service's running count never matches its desired count, or
tasks cycle in a start/stop loop.

**Cause:** the container is crashing on startup — usually a missing environment
variable, an IAM permission, or a code error.

**Fix:** open **ECS → your cluster → the failing service → Logs** and read the
CloudWatch output. Confirm the required environment variables are passed through
CDK and that the task role has the IAM permissions the service needs.

### CloudFront returns 403 Forbidden

**Cause:** CloudFront can't reach the S3 bucket, or the distribution hasn't
propagated yet.

**Fix:** wait 5–10 minutes after the frontend workflow completes — distributions
take time to deploy. If it persists, confirm the bucket policy grants CloudFront
OAC access and that the distribution's origin points at the correct bucket.

### API returns 502 Bad Gateway

**Cause:** the ALB can't reach a healthy ECS task.

**Fix:** confirm tasks are running, that the ALB target group shows **healthy**
targets, and that the service's security group allows traffic from the ALB.
Check the App API CloudWatch logs for startup errors.

## After deploy

### Login or first-boot page won't load

**Cause:** the App API isn't running, or the Cognito User Pool wasn't created.

**Fix:** confirm the App API ECS service is healthy and that `platform.yml`
completed (the User Pool is created there). On a fresh deploy you should see the
**first-boot setup** page — if you see a login page instead, first-boot may have
already been completed.

### Login succeeds but redirects to an error

**Cause:** a redirect-URI mismatch in the Cognito app client, or a federated
provider misconfiguration.

**Fix:** confirm the app client's callback URLs include your frontend domain
(e.g. `https://app.example.com/auth/callback`). For a federated provider, make
sure its app registration lists the Cognito domain as an allowed redirect URI.
The browser's Network tab shows the exact redirect being attempted.

### Agent doesn't respond to messages

**Cause:** the Inference API isn't running, or Bedrock model access isn't
enabled.

**Fix:** confirm the Inference API has running tasks, then open **Bedrock → Model
access** and enable the models referenced in your seed data. Confirm the
Inference API task role has `bedrock:InvokeModel`, and check its CloudWatch logs.

### Admin features aren't visible

**Cause:** your account doesn't hold the system-admin role.

**Fix:** on a fresh deploy, the first-boot user is admin automatically. Log out
and back in for a fresh token. If needed, check the users table to confirm the
record carries the `system_admin` role; for federated users, confirm their
Cognito groups include the admin role.

## Still stuck?

- Re-check every value against the
  [configuration reference](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/.github/ACTIONS-REFERENCE.md).
- Read the failed workflow's logs for the specific error.
- Check the relevant service's CloudWatch logs for runtime errors.
