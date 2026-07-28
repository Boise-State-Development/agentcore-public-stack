# AgentCore Gateway Token Exchange — Implementation Plan

**Status:** Proposed
**Audience:** Boise State auth owners, token-service maintainers, AgentCore platform maintainers, MCP server owners
**Goal:** Let BoiseState.ai call existing campus MCP servers and APIs as the signed-in user without teaching every service to validate Cognito tokens or changing legacy APIs that already trust token-service JWTs.

## Decision summary

Add a new, isolated OAuth token-exchange flow to token-service and use Amazon Bedrock AgentCore Gateway to obtain a short-lived, target-specific token-service JWT when an agent calls a campus MCP tool.

The first implementation will:

1. Keep the existing token-service login and refresh routes unchanged.
2. Add a feature-flagged RFC 8693 endpoint such as `POST /v2/oauth/token`.
3. Accept the user's enriched Cognito **access token** as the subject token.
4. Authenticate AgentCore/BoiseState.ai as an approved confidential client.
5. Issue the same token-service JWT format that existing campus APIs already accept.
6. Register token-service as an AgentCore Identity custom OAuth provider with OBO enabled.
7. Register campus MCP servers through the existing `mcp` Gateway plugin type using OAuth + Token Exchange.
8. Keep current application-role filtering for initial tool access. Add finer Gateway Policy controls in a later phase.

**OBO** means **On Behalf Of**: BoiseState.ai proves both which trusted application is calling and which authenticated employee it is acting for.

## Why this approach

Today, different parts of the environment trust different credentials:

- BoiseState.ai uses Cognito access tokens behind a proper BFF session.
- Existing campus applications and APIs trust token-service JWTs.
- AWS-hosted tools commonly use IAM/SigV4.
- Third-party connectors use AgentCore Identity's OAuth token vault.

Without a central exchange, each MCP server must learn Cognito validation or each legacy API must be changed. Token exchange moves that translation into one controlled place.

The result is:

- The browser never receives or handles the downstream token.
- MCP servers do not need to validate Cognito.
- Legacy APIs continue receiving the token format they already trust.
- Token-service remains the source of application-specific roles and audiences.
- New campus integrations follow one repeatable registration process.

## Related existing design

[`MCP_USER_IDENTITY_FORWARDING_SPEC.md`](./MCP_USER_IDENTITY_FORWARDING_SPEC.md) documents direct forwarding of an enriched Cognito access token to a controlled `mcp_external` server. That remains useful when a server is designed to trust Cognito directly.

This plan covers a different case: a server or legacy API that already trusts token-service and should sit behind AgentCore Gateway. It does not replace the direct-forwarding path.

## Current platform state

### Already implemented

- The SPA uses an httpOnly BFF session cookie; Cognito access, refresh, and ID tokens remain server-side in `BFFSessionsTable`.
- The BFF refreshes the Cognito access token and forwards it to inference-api.
- A Cognito Pre-Token-Generation v2 hook enriches the access token with the stable Boise State employee identifier.
- Inference-api has the current Cognito access token as `current_user.raw_token`.
- The MCP Gateway plugin model already supports:
  - `credential_type = oauth`
  - a credential-provider ARN
  - OAuth scopes
  - `grant_type = token_exchange`
- `GatewayTargetService` already maps that configuration to AgentCore's `OAUTH` + `TOKEN_EXCHANGE` target configuration.
- AgentCore Identity connector provisioning already handles custom OAuth providers, client credentials, and discovery metadata.

### Missing today

- token-service does not accept a Cognito access token. Its existing handler accepts an Entra authorization code or refresh token and then issues a token-service JWT.
- `AgentCoreRegistrar` does not provision `onBehalfOfTokenExchangeConfig` on a credential provider.
- The centralized Gateway uses `AWS_IAM` inbound authorization.
- The agent's Gateway MCP client signs requests with SigV4.
- Because the current Gateway receives IAM identity rather than a user JWT, it has no user subject token to exchange and no user claims for Cedar policy evaluation.
- There is no admin policy-management layer for AgentCore Gateway Policy.

The existing “Token Exchange” target option is therefore necessary but not sufficient by itself.

## Proposed request flow

```text
Employee
  |
  | Entra sign-in federated through Cognito
  v
BoiseState.ai BFF
  |  Browser holds only __Host-bff_session
  |  Server holds and refreshes Cognito tokens
  v
Inference API / Agent
  |  Cognito access token contains stable employee/sample ID
  |  Calls Gateway with that user access token
  v
AgentCore Gateway
  |  1. Validates the inbound Cognito JWT
  |  2. Identifies the selected MCP tool/target
  |  3. Sends an RFC 8693 OBO request to token-service
  v
Token-service POST /v2/oauth/token
  |  1. Authenticates the AgentCore client
  |  2. Validates the Cognito subject token
  |  3. Reads the stable employee/sample ID
  |  4. Resolves target application roles
  |  5. Issues a short-lived token-service JWT for that target
  v
AgentCore Gateway
  |  Adds Authorization: Bearer <token-service JWT>
  v
Campus MCP server / legacy API
  |  Validates the token it already understands
  |  Performs final business authorization
```

## Trust and responsibility boundaries

| Component | Responsibility |
|---|---|
| Cognito | Authenticate the user and issue the enriched access token used as proof of identity. |
| BoiseState.ai BFF | Keep Cognito tokens server-side, refresh them, and provide the current access token to the agent request. |
| AgentCore Gateway | Validate the inbound user token, select the MCP target, obtain the outbound target token, and enforce future tool policies. |
| token-service | Validate subject and client identity, resolve application roles, and issue audience-specific token-service JWTs. |
| MCP server | Validate the token-service JWT and avoid accepting unauthenticated direct calls. |
| Legacy API | Remain the final authority for business operations such as approving directory entry changes. |

Authentication and authorization remain separate:

- Token exchange proves **who is acting and for which target**.
- Tool/API authorization decides **what that person may do**.

## Phase 0 — Confirm contracts and choose the Gateway migration strategy

Before implementation, document these values:

- Cognito issuer URL and JWKS URL.
- BFF Cognito app client ID expected in access tokens.
- Exact enriched employee-ID claim name.
- token-service issuer, signing algorithm, and legacy audience naming convention.
- Pilot MCP target and corresponding token-service application ID/audience.
- AgentCore confidential client ID and secret ownership/rotation process.
- Pilot scopes and maximum token lifetime.

### Pilot target: Campus Directory API

The first integration uses the Campus Directory API — a .NET Core Lambda-backed service fronting a DynamoDB table of employee contact information. It already validates token-service JWTs via RSA public key and requires no code changes to accept an exchanged token.

| Contract Item | Directory API Value |
|---|---|
| Pilot MCP target | Campus Directory API (`/directory/*` endpoints) |
| token-service audience | The registered Directory application audience in token-service |
| Pilot scopes | Default (Directory uses role claims, not OAuth scopes) |
| Maximum token lifetime | 5–10 minutes |
| First pilot endpoint | `GET /directory/me` — proves employee identity flows correctly |
| Role-gated test endpoint | `POST /directory/pending` — requires IamStaff or DotNetDevelopers role |
| Auth mechanism | token-service JWT validated with RSA public key (no JWKS endpoint — keys are configured directly) |
| Dev base URL | `https://directory-api.dev.boisestate.edu` (confirmed) |

**Why Directory is the right first target:**

- Already trusts token-service JWTs — zero code changes on the target side.
- No VPC dependency — Lambda + DynamoDB, publicly reachable via API Gateway.
- Read-heavy and low-risk — worst case is a directory entry update, not corrupting business-critical state.
- Tiered authorization — anonymous search, authenticated self-lookup, and role-gated admin endpoints provide three levels of proof.
- Simple data model — employee contact info (name, email, phone, department, building/room, title).

### Gateway decision — one Gateway, migrate inbound to JWT (DECIDED)

**Decision: keep a single centralized Gateway and migrate its inbound authorizer from `AWS_IAM` to `CUSTOM_JWT` (Cognito).** Do not split Gateways by backend type.

#### Why one Gateway is the right endstate

The two halves of a Gateway behave differently, and only one of them is a single global setting:

| | Scope | Implication |
|---|---|---|
| **Outbound** (per-target credentials) | Per target | One Gateway can mix `GATEWAY_IAM_ROLE` (Wikipedia/ArXiv Lambdas), OAuth token-exchange (token-service .NET APIs), and OAuth token-vault (Google/Canvas) simultaneously. |
| **Inbound** (`authorizerType`) | **One per Gateway** | A single scalar — `CUSTOM_JWT`, `AWS_IAM`, `NONE`, or `AUTHENTICATE_ONLY`. There is no "accept either SigV4 or JWT" mode. |

The Gateway is the front desk; each target is a door with its own key. Backend
differences (MCP-native vs. legacy token-service API) are entirely an *outbound*
concern, so splitting Gateways along that seam would solve nothing while
doubling the catalog, target-service, SSM, and agent-client surface.

A second Gateway is justified only if some caller genuinely cannot present a
user JWT at the front desk. On this platform, none can't — see below.

#### Why the migration is safe here

Because Cognito is the platform's single token authority, every inbound path
already collapses to a Cognito access token regardless of how the user
originally authenticated. Verified against the code:

| Caller | Has a Cognito access token? | Notes |
|---|---|---|
| Browser / BFF session | Yes | By construction — the BFF holds and refreshes it server-side. |
| Scheduled & headless runs | Yes | `CognitoRefreshBearerAuth.mint_bearer_for_user()` (`apis/shared/harness/auth.py`) mints one from the user's stored headless grant. `run_agent_headless` requires it to start, so such a run cannot execute without a token *today* either — no regression. |
| API-key (`/chat/api-converse`) | N/A | Calls Bedrock Converse directly (`chat/converse_routes.py`); never touches the Gateway or tools. Out of scope. |

The only Gateway *data-plane* caller is the agent's `gateway_mcp_client.py`.
`gateway_identity.py` is control-plane target management via
`bedrock-agentcore-control` and is unaffected by inbound auth.
`external_mcp_client.py` is the separate direct-forwarding path.

#### The authorizer is immutable — confirmed the hard way

**An existing Gateway's `authorizerType` cannot be changed.** The first attempt
to deploy this migration failed mid-update:

```
Resource handler returned message: "Authorizer type cannot be updated for an
existing gateway (Service: BedrockAgentCoreControl, Status Code: 400)"
HandlerErrorCode: InvalidRequest
```

The stack rolled back cleanly (`UPDATE_ROLLBACK_COMPLETE`); the Gateway kept
`AWS_IAM`, `READY` status, and both registered targets.

**Why pre-deploy checks did not catch it.** Two sources said the change was
safe, and both were describing CloudFormation's model rather than the service's
validation:

| Signal | What it said | Why it was wrong |
|---|---|---|
| CFN resource reference | `AuthorizerType` → *"Update requires: No interruption"* | Describes CFN's update *mode*, not whether the service accepts the call |
| `cdk diff` (real change set) | `[~]` modify-in-place, no replacement | A change set predicts CFN's plan; it does not invoke service-side validation |

Treat this as the general lesson for AgentCore resources: **a change set is not a
deploy test.** For a young service, verify a mutation against a throwaway
resource before trusting either signal.

#### What this means for the migration

`gateway.inboundAuth` effectively sets the authorizer **at Gateway creation
time**. Moving an existing deployment from `AWS_IAM` to `CUSTOM_JWT` requires
creating a **new Gateway** and cutting over:

1. Create a second Gateway with `CUSTOM_JWT` (the existing one keeps serving).
2. Re-register every target on it. Targets are managed out-of-band — by app-api's
   `GatewayTargetService` (admin-registered) and by the `mcp-servers` repo — so
   this is not a CDK-only step and needs its own design.
3. Point the agent at the new Gateway (`/{prefix}/gateway/id`).
4. Verify, then delete the old Gateway.

So the plan's original "option B" (a second Gateway) is not a preference — it is
the **only available mechanism**. The single-Gateway *endstate* still holds: you
end with one Gateway, because the old one is deleted after cutover. What changes
is that getting there is a create-and-cutover, not a config flip.

Defaults were set accordingly: `gateway.inboundAuth` defaults to `iam` (matching
every deployed Gateway) and the agent's `AGENTCORE_GATEWAY_INBOUND_AUTH` defaults
to `iam` too, so a partial deploy cannot leave the agent presenting a credential
its Gateway rejects.

#### Critical authorizer-config gotcha

**Cognito *access* tokens carry `client_id`, not `aud`.** The
`customJWTAuthorizer` must therefore validate with **`allowedClients`** (the BFF
app client ID) — **not** `allowedAudience`. Setting `allowedAudience` against a
Cognito access token makes every Gateway call fail authorization. (The
`travel-auth` MCP server documents this same trap for its PyJWT check, where
`JWT_AUDIENCE` is deliberately left unset.)

## Phase 1 — Gateway spike and anonymous Directory tool

**Goal:** Prove the Gateway JWT-auth path works end-to-end and deploy a working `directory_search` tool *before* investing in token-service changes. This front-loads the riskiest unknowns (Gateway authorizer behavior, agent bearer-token wiring, MCP target connectivity) and gives you a working tool to show for it.

### 1A. Gateway inbound JWT authentication

Migrate the existing centralized Gateway's inbound authorizer from `AWS_IAM` to `CUSTOM_JWT` so it accepts Cognito access tokens instead of SigV4 (decided in Phase 0).

Update:

- `infrastructure/lib/constructs/gateway/agentcore-gateway-construct.ts`
- Infrastructure configuration and deployment inputs
- Gateway construct tests

Configure a custom JWT authorizer that trusts the Cognito user pool. Update outputs and documentation that currently state the Gateway requires SigV4.

Target shape:

```ts
authorizerType: 'CUSTOM_JWT',
authorizerConfiguration: {
  customJWTAuthorizer: {
    discoveryUrl: `https://cognito-idp.${region}.amazonaws.com/${userPoolId}/.well-known/openid-configuration`,
    // MUST be allowedClients, NOT allowedAudience — Cognito access tokens
    // carry `client_id`, not `aud`. See the Phase 0 gotcha.
    allowedClients: [bffAppClientId],
  },
},
```

Required config values (both already published to SSM):

- `/{prefix}/auth/cognito/user-pool-id`
- `/{prefix}/auth/cognito/bff-app-client-id`

This applies to a **newly created** Gateway only. An existing Gateway's authorizer cannot be changed — see "The authorizer is immutable" in Phase 0. For an environment whose Gateway already exists, leave `inboundAuth` at `iam` and follow the create-and-cutover path instead.

### 1B. Agent Gateway client — bearer token

Update:

- `backend/src/agents/main_agent/integrations/gateway_mcp_client.py`
- `backend/src/agents/main_agent/tools/gateway_integration.py`
- The agent construction path that already has `current_user.raw_token`
- Gateway integration tests

Replace SigV4 data-plane authentication for the JWT Gateway with bearer authentication using the current user's enriched Cognito access token.

Requirements:

- The token must be supplied per user and per agent invocation.
- Never cache one user's bearer token in a global/shared Gateway client.
- Keep Gateway clients scoped to the agent lifecycle.
- Preserve existing tool filtering and MCP Apps behavior.
- Return a clear authentication error when no user token is available.

Because there is a single Gateway (Phase 0 decision), SigV4 data-plane signing for the Gateway is removed rather than kept alongside a second client. Keep `gateway_identity.py` untouched — it is control-plane (`bedrock-agentcore-control`) target management and still uses IAM.

Note the behavioral change: today the agent signs Gateway calls with a machine identity, so any code path could reach Gateway tools. After the flip, a Gateway call requires a real user access token. Verified safe — scheduled/headless runs already mint one via `CognitoRefreshBearerAuth`, and the API-key path never touches the Gateway (see Phase 0).

### 1C. Deploy the anonymous `directory_search` tool

Register a Directory MCP target that uses **no outbound credential** (or Gateway IAM role only) and exposes only the anonymous `directory_search` endpoint:

```text
Protocol:             MCP Gateway (AgentCore)
Target name:          campus-directory
Endpoint URL:         <Directory API Lambda/API-Gateway URL>
Listing mode:         Default
Outbound credential:  GATEWAY_IAM_ROLE (no user token needed for anonymous endpoint)
```

Tool schema for this phase (single tool):

```json
[
  {
    "name": "directory_search",
    "description": "Search the Boise State employee directory by name, email, department, or title. Returns public contact information.",
    "inputSchema": {
      "type": "object",
      "required": ["search_terms"],
      "properties": {
        "search_terms": {
          "type": "string",
          "description": "Space-separated search terms (name, email, department, title)"
        },
        "page_size": {
          "type": "integer",
          "description": "Results per page (default 25, max 100)",
          "default": 25
        },
        "page": {
          "type": "integer",
          "description": "Page number (1-based)",
          "default": 1
        }
      }
    }
  }
]
```

#### MCP Lambda wrapper

Build a simple Lambda that receives the MCP tool invocation from Gateway, translates it to an HTTP request against the Directory API's `GET /directory/search?searchTerms=...` endpoint, and returns the response as MCP tool output. This follows the same pattern as existing Gateway Lambda tools (Wikipedia, ArXiv, etc.).

Because `directory_search` is `[AllowAnonymous]` on the Directory API, no Authorization header is needed. The wrapper is trivial.

### 1D. Observe the OBO request format (optional but recommended)

If Gateway is configured with a dummy OAuth OBO provider (pointing at a request-logging endpoint like a RequestBin or a Lambda that logs and returns 400), you can observe exactly what AgentCore sends as the token-exchange request. This tells you:

- The exact `grant_type` value AgentCore uses.
- Whether it sends `subject_token_type` and `audience`.
- How it formats `client_id`/`client_secret` (Basic header vs. POST body).
- Any AgentCore-specific parameters you didn't expect.

This intel directly informs the token-service v2 endpoint implementation in Phase 2.

### Phase 1 acceptance criteria

- A signed-in user's agent can invoke `directory_search` through the JWT-authenticated Gateway.
- The Gateway accepts the Cognito access token as a bearer token.
- `directory_search` returns employee contact results.
- Existing Gateway tools (Wikipedia, ArXiv, and every other registered target) still work after the inbound-auth migration — their outbound credentials are unchanged.
- The agent correctly passes per-user bearer tokens (no cross-user leakage).
- (Optional) The OBO request format has been captured and documented for Phase 2.

**At this point you have a working Directory search tool in the agent, the Gateway JWT path is proven, and you know exactly what token-service needs to accept.**

## Phase 2 — Add the isolated token-service v2 exchange endpoint

Implement a new feature module and leave the existing Entra authorization-code and refresh handlers unchanged.

Suggested structure:

```text
Features/
  TokenExchangeV2/
    ExchangeToken.cs
    CognitoTokenValidator.cs
    ClientAuthenticator.cs
    ApplicationTokenIssuer.cs
    Models.cs
    TokenExchangeV2Endpoints.cs
```

Suggested endpoint:

```http
POST /v2/oauth/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic <client-id:client-secret>

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<cognito-access-token>
subject_token_type=urn:ietf:params:oauth:token-type:access_token
audience=<target-application>
scope=<optional-target-scopes>
```

Expected response:

```json
{
  "access_token": "<token-service-jwt>",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
  "token_type": "Bearer",
  "expires_in": 600
}
```

### Validation requirements

The endpoint must:

- Be disabled by default behind a feature flag.
- Authenticate AgentCore/BoiseState.ai as a confidential client.
- Validate the Cognito token signature using Cognito JWKS.
- Require the exact Cognito issuer.
- Require the expected BFF app client.
- Require `token_use = access`.
- Validate expiration and not-before values.
- Require the configured employee-ID claim and validate its format.
- Permit only allowlisted target audiences for the calling client.
- Reject unsupported grant types and token types.
- Never log subject tokens, client secrets, or issued tokens.

### Issued token requirements

The new path should preserve the existing token-service contract:

- Same token-service issuer.
- Same audience format expected by the selected legacy application.
- Same RSA signing behavior and KMS-protected key source.
- Same application-role claim format.
- Same stable user identifier expected by current consumers.
- Lifetime no longer than the incoming Cognito token, with a short configured maximum such as 5–10 minutes.

For the pilot, duplicating the existing application lookup, role lookup, and signing sequence is acceptable to avoid refactoring the production login path. Mark the duplication explicitly and add contract tests. Consolidate only after the pilot succeeds.

### Operational controls

- Client-to-audience allowlist.
- Rate limiting.
- Dedicated metrics and structured logs.
- Independent kill switch.
- No external network calls during token-service startup.
- Dev deployment before production.
- Fast rollback by disabling the v2 feature.

## Phase 3 — Add OBO support to AgentCore connector provisioning

Extend the existing custom OAuth connector model rather than creating a new tool or plugin protocol.

Primary code areas:

- `backend/src/apis/shared/oauth/models.py`
- `backend/src/apis/shared/oauth/agentcore_registrar.py`
- `backend/src/apis/app_api/admin/oauth/routes.py`
- Admin connector form/models under `frontend/ai.client/src/app/admin/connectors/`
- Existing OAuth registrar and route tests

Add connector configuration for:

- OAuth mode: user federation, client credentials, or OBO token exchange.
- OBO grant type: initially `TOKEN_EXCHANGE`.
- Client authentication method: initially client-secret basic.
- Optional actor-token settings only if token-service later requires them.
- OAuth discovery URL or explicit authorization-server metadata.

For token-service, the AgentCore provider input should include the equivalent of:

```json
{
  "customOauth2ProviderConfig": {
    "oauthDiscovery": {
      "authorizationServerMetadata": {
        "token_endpoint": "https://token-service.example.edu/v2/oauth/token"
      }
    },
    "clientId": "boisestate-ai-agentcore",
    "clientSecret": "<managed-secret>",
    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
    "onBehalfOfTokenExchangeConfig": {
      "grantType": "TOKEN_EXCHANGE"
    }
  }
}
```

Use the exact AWS SDK shape supported by the repository's pinned boto3 version. Create and update must both send the full OBO configuration because AgentCore credential-provider updates are full replacements.

### Phase 3 acceptance criteria

- Admin can create a custom OBO connector for token-service.
- AgentCore stores the client secret; the platform DynamoDB record does not.
- The returned credential-provider ARN is persisted.
- Editing metadata without rotating credentials preserves the provider.
- Credential rotation requires the full client ID/secret pair, as it does today.
- Delete remains idempotent.

## Phase 4 — Wire authenticated Directory tools end-to-end

With Gateway JWT auth proven (Phase 1), token-service accepting OBO exchanges (Phase 2), and the AgentCore OBO connector registered (Phase 3), this phase connects the final dots: upgrade the Directory Gateway target to use OBO credentials and add the authenticated tool endpoints.

### 4A. Upgrade Directory target to OAuth OBO

Update the existing `campus-directory` Gateway target registration from `GATEWAY_IAM_ROLE` to OAuth token exchange:

```text
Protocol:             MCP Gateway (AgentCore)
Target name:          campus-directory
Endpoint URL:         <Directory API Lambda/API-Gateway URL>
Listing mode:         Default
Outbound credential:  OAuth
Credential provider:  <token-service OBO provider ARN from Phase 3>
Grant type:           Token Exchange
OAuth scopes:         (empty — Directory uses role claims)
```

### 4B. Add authenticated tools to the MCP Lambda wrapper

Extend the Directory MCP Lambda wrapper with tools that pass the OBO-exchanged Bearer token to the Directory API:

```json
[
  {
    "name": "directory_get_me",
    "description": "Get the calling user's own directory entry including private fields (office location, employee ID). Requires authentication.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    }
  },
  {
    "name": "directory_get_pending",
    "description": "Get pending directory entry change requests. Requires IamStaff or DotNetDevelopers role.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "page_size": {
          "type": "integer",
          "default": 25
        },
        "page": {
          "type": "integer",
          "default": 1
        }
      }
    }
  }
]
```

The wrapper Lambda reads the Authorization header that Gateway injects (the OBO-exchanged token-service JWT) and forwards it to the Directory API. For `directory_search`, the token is optional (the endpoint is anonymous); for `directory_get_me` and `directory_get_pending`, it is required.

### Phase 4 acceptance criteria

- Invoking `directory_get_me` causes AgentCore to call token-service's `/v2/oauth/token` with the user's Cognito access token.
- token-service receives a validated Cognito subject token and authenticated client identity.
- The Directory API receives a token-service JWT with the expected audience and the user's employee ID.
- `directory_get_me` returns the calling user's own directory entry (proves identity propagation end-to-end).
- `directory_search` continues to work (now optionally with a token, still anonymous-capable).
- `directory_get_pending` succeeds for a user with IamStaff/DotNetDevelopers role and is denied for users without that role.
- The Directory API accepts the exchanged token without any code changes.

## Phase 5 — Production hardening and reusable onboarding

After the pilot succeeds:

- Extract the duplicated token issuance sequence into a shared, tested `ApplicationTokenIssuer` used by old and new token-service flows.
- Add a standard onboarding checklist for each campus MCP target:
  - token-service application/audience registration
  - allowed AgentCore client-to-audience mapping
  - scopes
  - Gateway plugin registration
  - target JWT validation
  - business-authorization tests
- Add dashboards for exchange count, denials, latency, failures, and target audience.
- Add alarms for elevated token exchange failures and invalid subject tokens.
- Document client-secret rotation and signing-key rotation.
- Load-test token exchange and Gateway calls.
- Define availability behavior: fail closed when token-service is unavailable; do not fall back to service-wide credentials.

## Phase 6 — Fine-grained per-tool RBAC

### Initial control model

Keep the controls already present:

1. BoiseState.ai AppRoles determine whether a tool is visible/available.
2. User preferences determine whether an available tool is enabled.
3. token-service application roles are included in the target token.
4. The MCP server/legacy API performs final business authorization.

This is sufficient for the first OBO pilot.

### Gateway Policy capability

AgentCore Policy can evaluate every MCP tool call using:

- The inbound JWT subject and claims.
- The exact MCP tool name as the Cedar action.
- The Gateway ARN as the resource.
- Typed MCP tool arguments as `context.input`.

This supports rules such as allowing `directory_get_pending` only for users with the IamStaff role, or restricting `directory_add_entry` to the entry owner or a DotNetDevelopers member.

### Important claim-ordering rule

Gateway Policy evaluates the **inbound token used to call Gateway**. It cannot inspect the target-specific token-service JWT created later by outbound OBO.

During the first implementation, Gateway receives a Cognito token, so policy can use Cognito claims but not claims added only to the downstream token-service JWT.

If token-service must become the source of Gateway policy claims, add a later pre-Gateway exchange:

```text
Cognito access token
  -> token-service exchange for audience=agentcore-gateway
  -> short-lived token-service Gateway JWT with tool entitlement claims
  -> Gateway JWT authorizer + Cedar policy
  -> target-specific OBO exchange
  -> campus MCP/API
```

That design requires token-service to support exchanging a trusted token-service Gateway token for a target-specific token, or an equivalent delegated flow. Make this a separate architecture decision after the initial OBO path is proven.

### Directory API authorization example

The Directory API provides a clean three-tier authorization model for the pilot:

**Tier 1 — Public read (no policy needed):**

`directory_search` is anonymous-capable. Gateway Policy can allow it for any authenticated user without additional claim checks:

```cedar
permit(
  principal,
  action == Action::"directory_search",
  resource == GatewayTarget::"campus-directory"
);
```

**Tier 2 — Authenticated self-lookup:**

`directory_get_me` requires a valid identity (the employee ID from the exchanged token drives the query). No specific role is needed, but the user must be authenticated:

```cedar
permit(
  principal,
  action == Action::"directory_get_me",
  resource == GatewayTarget::"campus-directory"
) when {
  context.subject_token_valid == true
};
```

**Tier 3 — Role-gated admin operations:**

`directory_get_pending` requires the IamStaff or DotNetDevelopers role. Gateway Policy can deny early before the request reaches the Directory API:

```cedar
permit(
  principal,
  action == Action::"directory_get_pending",
  resource == GatewayTarget::"campus-directory"
) when {
  context.token_claims.roles.contains("IamStaff") ||
  context.token_claims.roles.contains("DotNetDevelopers")
};
```

**Key principles:**

- token-service governs entitlements such as `IamStaff` and `DotNetDevelopers` roles.
- Gateway Policy may deny the tool call before it reaches the target (fail-fast).
- The Directory API still performs its own role checks as the final authority — Gateway policy is an additional defense layer, not a replacement.
- Keep authorization tokens short-lived because claims are snapshots.
- For immediate revocation or highly volatile entitlement data, perform a live check in the target/API or a Gateway request interceptor rather than relying only on claims.

### Future policy-management work

The current admin UI stores per-tool approval flags but does not manage Cedar policies. A later feature should add:

- Policy engine creation/attachment to the Gateway.
- Policy templates keyed to Gateway target/tool names.
- Validation against the generated Gateway Cedar schema.
- Test/simulation before activation.
- Default-deny behavior for protected write tools.
- Audit logs containing user, tool, target, decision, and non-sensitive reason.

## Security requirements

- Never expose Cognito or token-service tokens to the browser.
- Never log raw tokens or client secrets.
- Authenticate both the user subject token and the calling AgentCore client.
- Restrict every client to explicitly allowed target audiences.
- Use short-lived target tokens.
- Validate issuer, signature, client/audience, `token_use`, and time claims.
- Preserve the original employee identity for audit.
- Do not copy arbitrary inbound claims into target tokens.
- Keep the target API as the final authorization authority for state-changing operations.
- Deny when token-service, AgentCore Identity, policy evaluation, or entitlement checks fail.

## Test plan

### token-service

- Valid Cognito access token exchanges successfully.
- ID token, expired token, wrong issuer, wrong app client, bad signature, or missing employee ID is rejected.
- Invalid client credentials are rejected.
- Client cannot request an unapproved audience.
- Issued JWT matches existing issuer/audience/role/signature contracts.
- Token expiry does not outlive the subject token or configured maximum.
- Feature-disabled route is unavailable.
- Existing authorization-code and refresh tests remain unchanged and passing.

### AgentCore provider registration

- Create/update payload includes OBO config.
- Custom discovery and explicit metadata both work.
- Full config is preserved on update.
- Secrets are never returned or stored in the platform table.

### Gateway and agent

- JWT-authorized Gateway accepts the expected Cognito access token.
- Missing, expired, or wrong-client tokens are rejected.
- User tokens cannot leak between agent instances.
- Existing target discovery and filtering still work.
- OAuth token-exchange target payload matches AWS API expectations.
- End-to-end request arrives at the pilot MCP server with the expected token-service JWT.

### Authorization

- AppRole without tool access cannot select the tool.
- User with tool access but no downstream role is denied by the target/API.
- Future Cedar policy denies protected tools without the required claim.
- Protected business action is still denied by the API when its live rules fail.

## Rollout sequence

1. Confirm Phase 0 contracts (Cognito issuer, employee-ID claim, token-service audience for Directory).
2. ~~Decide: migrate existing Gateway or add a second JWT Gateway.~~ Decided: single Gateway, migrate inbound to `CUSTOM_JWT`.
3. Deploy Gateway JWT authorizer (Phase 1A) in development.
4. Update agent Gateway client to pass bearer token (Phase 1B).
5. Build and deploy the Directory MCP Lambda wrapper with `directory_search` only (Phase 1C).
6. Validate: agent can search the directory via Gateway with JWT auth. First working tool.
7. (Optional) Observe the OBO request format via a logging endpoint (Phase 1D).
8. Implement token-service v2 exchange endpoint behind a disabled feature flag (Phase 2).
9. Deploy to token-service development environment.
10. Test direct RFC 8693 exchange with a non-production Cognito token and Directory audience.
11. Add AgentCore OBO provider support in the platform (Phase 3).
12. Register a development token-service OBO provider.
13. Upgrade Directory Gateway target to OAuth OBO credentials (Phase 4A).
14. Add `directory_get_me` and `directory_get_pending` to the MCP Lambda wrapper (Phase 4B).
15. Validate: `directory_get_me` returns the calling user's own entry (identity flows end-to-end).
16. Validate: `directory_get_pending` succeeds for IamStaff, denied for others (roles flow correctly).
17. Validate: identity, audit logs, failure behavior, and token expiry.
18. Run security review and load test.
19. Enable in production for an allowlisted group and audience.
20. Expand target-by-target; do not bulk-migrate existing integrations.

## Rollback

- Disable token-service `TokenExchangeV2` feature flag.
- Disable or remove the pilot Gateway target.
- Disable the token-service AgentCore credential provider.
- Restore the prior Gateway authorizer/client if the existing Gateway was migrated.
- Existing token-service login and refresh routes remain available throughout.
- Existing `mcp_external` direct-forwarding integrations remain unchanged.

## Out of scope for the first release

- Replacing existing token-service login flows.
- Changing all legacy APIs to trust Cognito.
- Migrating every `mcp_external` integration to Gateway.
- Building the full Cedar policy-management UI.
- Encoding volatile business state into long-lived Cognito claims.
- Removing final authorization checks from MCP servers or legacy APIs.

## Open decisions

1. ~~Migrate the existing IAM Gateway or add a second delegated JWT Gateway?~~ **Resolved by constraint: a second (new) Gateway, because option A is impossible.** The authorizer is immutable after creation, so an existing Gateway cannot be flipped — the first deploy attempt failed with "Authorizer type cannot be updated for an existing gateway". The single-Gateway *endstate* still holds (the old Gateway is deleted after cutover); the mechanism is create-and-cutover, not a config flip. See "The authorizer is immutable" in Phase 0.
2. Use OAuth discovery from token-service or explicit AgentCore authorization-server metadata?
3. Exact confidential-client authentication and secret-rotation process?
4. ~~Pilot MCP server and legacy audience?~~ **Decided: Campus Directory API.** Audience = the Directory application's registered token-service audience. See Phase 0 for rationale.
5. Initial scopes and target-token lifetime?
6. Long-term Gateway policy claims: Cognito claims, pre-Gateway token-service JWT, or live interceptor lookup?

## Definition of done for the pilot

The pilot is complete when an allowlisted BoiseState.ai user can invoke one campus MCP tool through AgentCore Gateway, Gateway silently exchanges the user's enriched Cognito access token through token-service, the target receives a short-lived application-specific token-service JWT, the legacy API accepts it without modification, unauthorized users are denied, no token reaches the browser, and existing token-service login flows continue to operate unchanged.
