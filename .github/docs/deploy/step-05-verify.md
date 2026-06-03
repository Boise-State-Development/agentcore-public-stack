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

### 5. (Optional) MCP Apps dogfood — end-to-end

Run this once you've registered an MCP-Apps-capable server (see [Register an MCP-Apps-capable MCP server](./step-04-deploy.md#register-an-mcp-apps-capable-mcp-server)). The sandbox proxy is **always provisioned** as part of `PlatformStack` and the host renderer is on by default (`AGENTCORE_MCP_APPS_HOST_ENABLED=true`) — provided `CDK_MCP_SANDBOX_CERTIFICATE_ARN` was set at deploy (see [Step 3b](./step-03-github-config.md#3b-deployment-variables)). It is the manual e2e scenario for the host-renderer initiative and walks every host↔App interaction. Using the `budget-allocator-server` example from the runbook:

**Setup**
- [ ] `budget-allocator-server` running over Streamable HTTP and registered as an `mcp_external` tool, granted to your role
- [ ] `AGENTCORE_MCP_APPS_HOST_ENABLED=true` (default) and `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` resolves to the deployed `mcp-sandbox.{domain}` origin
- [ ] Your SPA origin is in the sandbox's CSP `frame-ancestors`

**Scenario** — in a fresh chat, ask the agent to "help me allocate a budget" (or anything that invokes the tool):

- [ ] **Resource fetch** — a `tool_use` then `tool_result` card appears; backend logs show a server-side `resources/read` for the tool's `ui://…` resource (no client fetch)
- [ ] **Iframe render** — the App renders *inside* the tool card: a `ui_resource` SSE event arrives with a **non-empty** `sandboxOrigin`, and the iframe is sourced from that origin (not `srcdoc` against the SPA origin)
- [ ] **Tool-input push** — the App shows the arguments the model called it with (host pushed `ui/notifications/tool-input` from the active stream)
- [ ] **App-initiated `tools/call`** — drive the form (move a slider / pick a preset) so the App calls a server tool; the call shows up as its own tool card in the thread *and* the App updates from the `ui/notifications/tool-result` it gets back
- [ ] **`ui/update-model-context` mutates the next turn** — after changing the allocation, send a new chat message that asks about it (e.g. "is my current split reasonable?"); the model's reply reflects the App's latest state — i.e. context written via `ui/update-model-context` was merged into the **next** turn (not the one that opened the App)
- [ ] **`ui/open-link` consent prompt** — trigger a link-open from the App (e.g. an "industry benchmarks" link); an inline consent prompt appears in the message list (modeled on the OAuth-consent prompt) and the link only opens after you approve. (Consent is **frontend-only** — there is no `ui_consent_required` SSE event; don't look for one.)

<details>
<summary>The App card appears but the iframe is blank</summary>

In order of likelihood:
1. **The iframe shows a `chrome-error://chromewebdata/` page / `mcp-sandbox.{domain}` doesn't resolve (DNS `NXDOMAIN`).** `CDK_MCP_SANDBOX_CERTIFICATE_ARN` was unset when `PlatformStack` deployed, so the proxy fell back to the CloudFront default domain with no Route 53 ALIAS — and the SPA frames a host that doesn't exist. Set the cert var (see [Step 3b](./step-03-github-config.md#3b-deployment-variables)) and redeploy `PlatformStack`. A domained deploy missing this cert now fails at `cdk synth`, so a *fresh* deploy can't reach this state silently — it's mainly a concern for environments deployed before that guard landed. Confirm with `nslookup mcp-sandbox.{CDK_DOMAIN_NAME}`.
2. The SPA origin isn't in the sandbox CSP `frame-ancestors` → the browser blocks the frame (console shows a `frame-ancestors` violation). Add it via `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS` (e.g. `http://localhost:4200` for a local SPA) and redeploy `PlatformStack`.
3. The server didn't return `_meta.ui` on `tools/list`, or its `ui://` resource isn't `text/html;profile=mcp-app` → it isn't actually MCP-Apps-capable; re-check with the discover endpoint and the server's own logs.

</details>

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
