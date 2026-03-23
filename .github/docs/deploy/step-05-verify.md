# Step 5 of 5 — Verify Deployment

✅ Step 1: Prerequisites<br>
✅ Step 2: AWS Setup<br>
✅ Step 3: Configure GitHub<br>
✅ Step 4: Deploy Workflows<br>
➡️ **Step 5: Verify Deployment** ← You are here

⏱️ ~5 minutes · 🟢 Easy

---

All workflows have completed. Let's verify everything is working.

## Verification Checklist

### 1. Frontend Loads

Open your frontend URL in a browser (e.g. `https://app.example.com`).

- [ ] The page loads without errors
- [ ] You see a first-boot setup page (on fresh deployment) or a login page (if already set up)

> [!NOTE]
> CloudFront distributions can take a few minutes to fully propagate after the first deploy. If you get a 403 or "distribution not found" error, wait 5 minutes and try again.

### 2. First-Boot Setup (Fresh Deployment)

On a fresh deployment, you'll see the first-boot setup page. Create your admin account.

- [ ] Enter a username, email, and password
- [ ] Submit the form — you should be redirected to the login page
- [ ] Log in with your new credentials
- [ ] You land on the chat interface

<details>
<summary>First-boot setup fails</summary>

Common causes:
- **Password too weak:** Password must be at least 8 characters with uppercase, lowercase, number, and special character
- **ECS service not running:** Check that the App API service is healthy in the ECS console
- **DynamoDB permissions:** Verify the App API task role has write access to the DynamoDB tables

Check CloudWatch logs for the App API service for specific error details.

</details>

### 3. Agent Responds

Send a test message in the chat (e.g. "Hello, what can you help me with?").

- [ ] You see a streaming response from the agent
- [ ] The response completes without errors

<details>
<summary>Messages aren't getting responses</summary>

Check these in order:
1. **ECS services running:** In the AWS Console, go to ECS → your cluster → verify both the App API and Inference API services show "Running" tasks
2. **Bedrock model access:** Ensure your AWS account has access to the Bedrock models configured in the default model seed data
3. **Logs:** Check CloudWatch logs for the Inference API service for error details

</details>

### 4. Admin Access

The user who completed the first-boot setup is automatically the system admin.

- [ ] Navigate to the admin section
- [ ] You can see and manage models, tools, and roles

> [!TIP]
> To add federated identity providers (Entra ID, Okta, Google, etc.), use the admin dashboard's authentication settings. No redeployment is needed.

---

## You're Done!

Your AgentCore Public Stack is deployed and running. Here's what you have:

| Service | URL |
|---------|-----|
| Frontend | `https://app.example.com` |
| API | `https://api.example.com` |

### What's Next

- **Customize models:** Use the admin panel to add or modify available AI models
- **Add tools:** Configure additional MCP tools through the admin interface
- **Manage users:** Set up roles and permissions for your team
- **Monitor costs:** Review usage and cost tracking in the admin dashboard
- **Scale up:** Adjust ECS task counts and sizes via the [configuration variables](../../ACTIONS-REFERENCE.md)

---

## Need Help?

- [Troubleshooting Guide](./troubleshooting.md) — common issues and solutions
- [Full Configuration Reference](../../ACTIONS-REFERENCE.md) — all available settings
- [Back to Overview](../../README-ACTIONS.md) — deployment hub page

---

### 🔧 [Troubleshooting](./troubleshooting.md)
