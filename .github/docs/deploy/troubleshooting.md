# Troubleshooting

> Having issues? Find your symptom below. Each section is organized by deployment phase.

---

## AWS Setup Issues

<details>
<summary>ACM certificate stuck in "Pending validation"</summary>

**Symptom:** Certificate status stays "Pending validation" for more than 10 minutes.

**Cause:** DNS validation records are missing or not propagated.

**Fix:**
1. In the ACM Console, expand the certificate and look for the CNAME validation records
2. If using Route 53, click **"Create records in Route 53"** — ACM does this automatically
3. If using an external DNS provider, manually add the CNAME records shown by ACM
4. Verify with: `dig CNAME _acme-challenge.example.com`
5. Wait up to 5 minutes after records are in place

</details>

<details>
<summary>Route 53 hosted zone not resolving</summary>

**Symptom:** `dig NS example.com` doesn't return Route 53 nameservers.

**Cause:** Domain nameservers haven't been updated to point to Route 53.

**Fix:**
1. Go to Route 53 → Hosted zones → your zone
2. Copy the 4 NS records
3. Go to your domain registrar and update the nameservers to match
4. Wait up to 48 hours for propagation (usually much faster)

</details>

---

## GitHub Actions Failures

<details>
<summary>Workflow fails with "credentials" or "authentication" error</summary>

**Symptom:** Workflow fails early with an AWS authentication error.

**Cause:** AWS credentials are missing or incorrect.

**Fix:**
- **OIDC:** Verify `AWS_ROLE_ARN` secret is set and the IAM role trusts your GitHub repo's OIDC provider
- **Access keys:** Verify both `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` secrets are set correctly
- Check that the IAM role/user has sufficient permissions

</details>

<details>
<summary>Workflow fails with "variable not found" or empty values</summary>

**Symptom:** Workflow fails referencing an undefined or empty variable.

**Cause:** A required GitHub variable is missing.

**Fix:**
1. Go to **Settings → Secrets and variables → Actions → Variables**
2. Compare your variables against the [required list in Step 3](./step-03-github-config.md#3b-deployment-variables)
3. Add any missing variables
4. Re-run the failed workflow

</details>

<details>
<summary>Infrastructure deploy fails with "resource already exists"</summary>

**Symptom:** CDK deployment fails because a resource name conflicts.

**Cause:** The `CDK_PROJECT_PREFIX` you chose conflicts with existing AWS resources.

**Fix:**
- Choose a more unique `CDK_PROJECT_PREFIX` value
- Or delete the conflicting resources in AWS if they're from a previous failed deployment

</details>

<details>
<summary>CDK deploy fails with "no hosted zone found"</summary>

**Symptom:** Stack deployment fails with an error about Route 53 hosted zone not found.

**Cause:** `CDK_HOSTED_ZONE_DOMAIN` doesn't match an existing hosted zone, or the hosted zone is in a different AWS account.

**Fix:**
1. Verify the hosted zone exists in Route 53 in the same account
2. Ensure `CDK_HOSTED_ZONE_DOMAIN` matches the zone name exactly (e.g. `example.com`, not `www.example.com`)

</details>

---

## Deployment Issues

<details>
<summary>ECS service tasks keep restarting</summary>

**Symptom:** ECS service shows tasks in a start/stop loop, or "desired count" never matches "running count."

**Cause:** Container is crashing on startup. Common reasons: missing environment variables, incorrect IAM permissions, or a code error.

**Fix:**
1. Go to ECS → your cluster → the failing service → **Logs** tab
2. Check CloudWatch logs for error messages
3. Verify all required environment variables are passed through CDK
4. Check that the ECS task role has required IAM permissions

</details>

<details>
<summary>CloudFront returns 403 Forbidden</summary>

**Symptom:** Visiting your frontend URL returns a 403 error.

**Cause:** CloudFront can't access the S3 bucket, or the distribution hasn't propagated yet.

**Fix:**
1. Wait 5-10 minutes after the Frontend workflow completes — CloudFront distributions take time to deploy
2. If it persists, check that the S3 bucket policy grants CloudFront OAI/OAC access
3. Verify the CloudFront distribution's origin points to the correct S3 bucket

</details>

<details>
<summary>API returns 502 Bad Gateway</summary>

**Symptom:** API calls to `api.example.com` return 502.

**Cause:** The ALB can't reach the ECS service, or the service isn't healthy.

**Fix:**
1. Check ECS service health — ensure tasks are running
2. Check the ALB target group — targets should be "healthy"
3. Review CloudWatch logs for the App API service
4. Verify the service's security group allows traffic from the ALB

</details>

---

## Post-Deploy Issues

<details>
<summary>Login page doesn't load or shows an error</summary>

**Symptom:** The login page doesn't load, or the first-boot setup page doesn't appear on a fresh deployment.

**Cause:** The App API may not be running, or the Cognito User Pool wasn't created properly.

**Fix:**
1. Check that the App API ECS service is running and healthy
2. Verify the Infrastructure workflow completed successfully (Cognito User Pool is created there)
3. Check CloudWatch logs for the App API service for specific errors
4. If on a fresh deployment, ensure you see the first-boot setup page — if you see a login page instead, first-boot may have already been completed

</details>

<details>
<summary>Login succeeds but redirects to an error</summary>

**Symptom:** Login works, but the redirect back to the app fails.

**Cause:** Redirect URI mismatch in the Cognito App Client configuration, or a federated identity provider misconfiguration.

**Fix:**
1. Verify the Cognito App Client callback URLs include your frontend domain (e.g., `https://app.example.com/auth/callback`)
2. If using a federated provider, check that the provider's app registration includes the Cognito domain as an allowed redirect URI
3. Check your browser's developer console (Network tab) for the exact redirect URL being used

</details>

<details>
<summary>Agent doesn't respond to messages</summary>

**Symptom:** You can log in and see the chat UI, but messages get no response or time out.

**Cause:** The Inference API isn't running, or Bedrock model access isn't enabled.

**Fix:**
1. Check ECS — verify the Inference API service has running tasks
2. In the AWS Console, go to **Bedrock → Model access** and ensure you've enabled the models you want to use
3. Check CloudWatch logs for the Inference API service for specific errors
4. Verify the Inference API's task role has `bedrock:InvokeModel` permissions

</details>

<details>
<summary>Admin features aren't visible</summary>

**Symptom:** You're logged in but don't see admin menu items.

**Cause:** Your account doesn't have the system admin role.

**Fix:**
1. If this is a fresh deployment, the user who completed the first-boot setup should automatically have admin access
2. If using a federated identity provider, verify that the user's Cognito groups include the admin role
3. Log out and log back in to get a fresh token
4. Check the Users DynamoDB table to verify the user record has the `system_admin` role

</details>

---

## Still Stuck?

- Double-check all values against the [Full Configuration Reference](../../ACTIONS-REFERENCE.md)
- Review the GitHub Actions workflow logs for specific error messages
- Check AWS CloudWatch logs for runtime errors
- [Back to Overview](../../README-ACTIONS.md)
