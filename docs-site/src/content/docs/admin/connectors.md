---
title: Connectors
description: OAuth providers that tools, file sources and export targets use to act on a user's behalf, backed by AgentCore Identity.
sidebar:
  order: 7
---

AI Configuration group. A **connector** is an OAuth provider (Google,
Microsoft, GitHub and others) that the platform signs users in to on their own
behalf, so a tool can call that vendor's API as the user. Managed under
`/admin/connectors`, scope `admin.connectors`. There is no feature flag.

A connector does nothing by itself. Something has to use it:

| Used by | Set where |
|---|---|
| An external MCP server whose tools act as the user | **Required OAuth provider** on the server, in Admin → [Tools](/agentcore-public-stack/admin/tools/) |
| A built-in tool that calls a vendor API as the user | The same field on the built-in tool (for example the [meeting action item](/agentcore-public-stack/integrations/meeting-action-items/) tools) |
| A file source: importing files into an agent's or project's knowledge base, and Drive folder attachments | **File Source** on the connector |
| An export target: saving a conversation from the chat **Save to…** dialog | **Export Target** on the connector |

## How AgentCore Identity backs a connector

Each connector is split between two stores:

| Held by | What |
|---|---|
| AgentCore Identity (an OAuth2 credential provider) | Client ID, client secret, the vendor's endpoints, the callback URL |
| The platform's DynamoDB record | Display name, icon, scopes, allowed roles, enabled flag, custom OAuth parameters, adapter mappings |

The platform never stores the client secret. AgentCore Identity keeps it and
returns only its ARN.

Users' tokens live in AgentCore Identity's **token vault**, keyed by a workload
identity, the user and the provider. The stack creates one shared workload
identity (`{prefix}-platform-workload`) that both the App API and the AgentCore
Runtime act as. That is why an account a user connects on the Customize page
is the same one the agent uses in a conversation.

## Adding a connector

**New connector** asks for a type first:

| Type | Notes |
|---|---|
| Google | No default scopes. Add `access_type=offline` under Custom OAuth Parameters, or Google issues no refresh token, and usually `prompt=consent`, so a refresh token is issued again when a user reconnects |
| Microsoft | Defaults to `openid email profile offline_access`. See [Microsoft (Entra ID)](#microsoft-entra-id) |
| GitHub | Defaults to `read:user user:email` |
| Slack, Salesforce, Zoom | AgentCore Identity knows their endpoints; supply credentials and scopes |
| Custom (OIDC) | Any OpenID Connect provider. Needs an **OIDC Discovery URL**, which AgentCore fetches to find the endpoints |

Then the form:

| Field | What it does |
|---|---|
| Connector ID | Unique, lowercase letters, numbers and hyphens, up to 64 characters. It names the AgentCore credential provider and cannot be changed later |
| Display name, icon | What users see. An uploaded icon (PNG, JPEG, GIF, WebP or SVG, up to 100 KB) replaces the default |
| Connector Enabled | A disabled connector is hidden from every user |
| Client ID, Client Secret | From the OAuth app you registered at the vendor |
| Scopes | Comma-separated. What the vendor asks the user to grant |
| Custom OAuth Parameters | One `key=value` per line, passed to AgentCore Identity as `customParameters` on every consent and token request (for example `hd=example.com` to limit Google to one Workspace domain) |
| Allowed Roles | Which app roles may use the connector. **Allow all roles** leaves it unrestricted |
| File Source, Export Target | Optional adapter mappings. Google Drive is the adapter available for both today |

On **Create**, the platform registers the credential provider with AgentCore
Identity, then saves its own record. The next screen shows the **callback
URL**: add it to the OAuth app's authorized redirect URIs at the vendor (Google
Cloud Console, Microsoft Entra, GitHub OAuth App settings). Until you do, users
get an error from the vendor when they try to connect. The edit page keeps
showing the callback URL and the credential provider ARN.

### Editing and rotating credentials

AgentCore Identity's update replaces the provider's whole configuration and
never returns the secret, so:

- To rotate credentials, enter **both** the client ID and the client secret.
  Leave both blank to keep the existing ones.
- Changing a custom connector's discovery URL, or a Microsoft connector's
  tenant, also requires re-entering both.
- Metadata edits (name, icon, scopes, roles, enabled, parameters) need neither.

Changing scopes means users are asked to consent again the next time they need
the connector. Custom OAuth parameters are part of the vault's key, which is
why the stored set is sent identically on every request.

### Deleting

Deleting a connector deletes its AgentCore credential provider, and with it
every user token stored for it. If you recreate it, connected users have to
connect again.

## Linking an external MCP server

In Admin → Tools, open the external MCP server and, under **User OAuth
connector**, set **Required OAuth provider** to the connector. On each tool
call the runtime fetches the user's token for that connector from the vault
and sends it to the server as a bearer token. A user who has not connected yet
is asked to (see below).

A server uses one way of carrying the user's identity: a connector, forwarding
the user's own sign-in token, or a token exchange. They are mutually exclusive.

**Discover from server** on an OAuth-gated server runs as the signed-in admin,
so connect your own account to the connector first.

## How users connect

Users manage their accounts on **Customize → Connectors**
(`/customize/connectors`; the old `/settings/connectors` redirects there). The
page lists the enabled connectors their roles allow, with **Connect**, a
**Connected** badge and **Disconnect**. The Tools tab marks which tools need a
connection.

**Connect** opens the vendor's consent page in a popup. AgentCore Identity
handles the OAuth exchange on its callback URL, then returns the user to the
app's `/oauth-complete` page, which tells the opener and closes. The App API
then calls `CompleteResourceTokenAuth`, which is what stores the token in the
vault.

**Disconnect** cannot delete the token: AgentCore Identity has no per-user
vault delete. The platform records the disconnect instead, and every later use
asks for consent again and replaces the vault entry. The grant stays valid at
the vendor until it expires or the user removes the app in their account
settings there, which the confirmation dialog says.

The user-facing routes are under `/connectors` on the App API (list, status,
`initiate-consent`, `complete-consent`, and `DELETE /connectors/{id}/connection`).

## Consent in the middle of a conversation

Users do not have to visit Customize first. When a turn needs a connector the
user has not connected, the stream sends an `oauth_required` event after
`message_stop`, one per provider, with `{providerId, authorizationUrl,
interruptId?}`. The SPA shows a **Connect** prompt on the message. It comes in
two kinds:

| Kind | When | What happens after consent |
|---|---|---|
| Interrupt-driven (`interruptId` set) | The tool was registered and the user called it. The consent hook paused the turn before the call | The SPA posts the interrupt id back and the **same turn resumes** with the tool call |
| Pre-flight (no `interruptId`) | The MCP server refused the agent's `tools/list` with a 401 or 403, so its tools never registered | Nothing was paused, so nothing resumes. The next turn finds the token and the tools come back |

A pre-flight prompt is sent again on every turn until the user connects. The
agent is also told, in its system prompt, which servers were dropped, so it does
not invent their tools. Before sending the event, the runtime asks the vault
directly: if the user already has a token there, it uses it and retries the
pre-flight instead of prompting. Only a 401 or 403 counts; an unreachable server
is not reported as needing consent.

The SPA deduplicates prompts by provider and remembers a dismissed pre-flight
prompt for the tab session. See [SSE Streaming](/agentcore-public-stack/features/sse-streaming/)
for the full event list.

## Microsoft (Entra ID)

A Microsoft connector has one setting no other vendor has: **Directory (tenant) ID**. Set it to the app registration's directory (tenant) ID or one of the tenant's verified domains (`contoso.onmicrosoft.com`), both shown on the registration's Overview page. A single-tenant registration, which is what Entra creates by default, can only sign users in through its own tenant, so set the field for one. Leave it empty only for a multi-tenant registration.

The tenant is part of the credential config AgentCore Identity holds, so changing or clearing it on an existing connector requires re-entering the client ID and secret, the same as a discovery URL change.

Microsoft issues an access token for **one resource at a time**. A connector whose scopes are for Microsoft Graph (`User.Read`, `Calendars.Read`) cannot also call another Microsoft API. For one that can, such as Azure DevOps, create a separate connector whose scopes name that resource, for example `499b84ac-1321-427f-aa17-267ca6975798/user_impersonation` plus `offline_access`, and grant that delegated permission on the app registration.
