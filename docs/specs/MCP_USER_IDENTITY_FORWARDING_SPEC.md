# MCP User Identity Forwarding — Decision Doc

**Status:** Accepted — platform enrichment IMPLEMENTED (public stack, disabled by
default). MCP-server consumption (items 3–4) pending in the mcp-servers repo.
**Audience:** Auth architecture owner, platform maintainers
**Scope:** How the platform forwards the authenticated end user's identity to
personalized MCP tools, so a tool can answer "who is the current user?" and
scope data to them.

> **Implementation note (this repo, agentcore-public-stack).** Option A is built
> and shipped disabled-by-default:
> - Config: `mcpIdentity.tokenEnrichment` in `infrastructure/lib/config.ts`
>   (`enabled` default `false`; claim map via context or the
>   `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS` JSON env var). Inert block in
>   `cdk.context.json`.
> - Handler: `infrastructure/lambda-assets/token-enrichment/handler.py`
>   (stdlib-only, fail-open) + co-located pytest.
> - Construct: `infrastructure/lib/constructs/identity/token-enrichment-construct.ts`,
>   wired conditionally into `platform-stack.ts`. The Cognito pool's
>   `featurePlan` is pinned to `ESSENTIALS`.
> - Workflow: `CDK_MCP_TOKEN_ENRICHMENT_ENABLED` + `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS`
>   job-level env in `platform.yml`.
>
> Items 3–4 (server-side token validation + `student-myboisestate` cutover) live
> in the separate mcp-servers repo — see "Follow-on: mcp-servers repo" below.

---

## Problem

Personalized MCP servers (e.g. `student-myboisestate`) need to identify the
logged-in user to fetch that user's data from a downstream system (MyBoiseState
/ Boomi keys on the 9-digit **employee number**). Today the tool fails with:

```
No authenticated user found. Please ensure the Authorization header
contains a valid Bearer token.
```

even with the "Forward user's OIDC token to MCP server" checkbox enabled.

## Context: how identity flows today (verified)

Request path (SPA → agent → MCP tool):

```
SPA --(session cookie)--> app-api /chat proxy
     --(Authorization: Bearer <ACCESS token>)--> inference-api (AgentCore Runtime)
     --(forward_auth_token → Authorization: Bearer <ACCESS token>)--> MCP server
```

Key findings (all verified against the running dev environment):

1. **The token forwarded end-to-end is the Cognito _access_ token.**
   - `app-api` proxy sets `Authorization: Bearer {current_user.raw_token}`
     (`apis/app_api/chat/proxy_routes.py`).
   - `raw_token` = `record.cognito_access_token`
     (`apis/shared/auth/dependencies.py`).
   - `inference-api` re-derives `raw_token = token` from the forwarded header
     (`get_current_user_trusted`), and the agent forwards that to the MCP server.

2. **The access token does NOT carry identity claims.** Decoded live token:
   `sub` (Cognito UUID), `username` (`ms-entra-id_...`), `client_id`, `scope`,
   `token_use`. No `email`, `name`, `custom:roles`, or `custom:provider_sub`.

3. **The ID token DOES carry them** (via Cognito attribute mapping). Decoded live
   ID token for a federated user included `custom:provider_sub: "113124161"`
   (the employee number), `custom:roles`, `email`, `name`.

4. **The ID token is NOT available at agent-request time.** It stays in the BFF
   session record (`record.id_token`) in app-api and is never threaded to the
   inference-api or the agent. Only the access token makes the trip.

5. **The employee number source exists on federated users.** Cognito custom
   attribute `custom:provider_sub` is populated for `ms-entra-id_*` users
   (confirmed for 3 users), absent for native Cognito users.

6. **Cognito pool is on the ESSENTIALS tier** (`describe-user-pool` →
   `UserPoolTier: ESSENTIALS`), which supports Pre-Token-Generation **v2**
   access-token customization. No triggers currently configured
   (`LambdaConfig: {}`).

### Why not the Gateway

AgentCore Gateway targets with `GATEWAY_IAM_ROLE` authenticate as the *gateway*
(SigV4) and do not forward the end user's token. So **personalized tools cannot
go through the Gateway** — they must be `mcp_external` + Function URL
`AuthType: NONE` + `forward_auth_token`, and validate the forwarded token
in-app. Gateway remains the right home for non-personalized/shared tools
(policy-search, grants-gov, arxiv, semantic-scholar).

---

## Options considered

**A. Pre-Token-Generation v2 hook — enrich the access token (RECOMMENDED)**
A Cognito trigger copies configured attributes (e.g. `custom:provider_sub`) into
named claims (e.g. `employee_number`) on the **access token**. The token already
flows end-to-end, so no handoff plumbing changes. Requires ESSENTIALS/Plus (have
it). Blast radius: runs on every token issuance pool-wide (mitigated by a
trivial, fail-safe Lambda).

**B. Forward the ID token instead of the access token**
The ID token already has the claims, but is not present at agent-request time.
Would require threading a second token through app-api → Runtime → agent → MCP
client without disturbing the Runtime's inbound auth (which validates the access
token). More plumbing across a security boundary.

**C. Per-server lookup (AdminGetUser / DynamoDB)**
Each MCP server re-derives identity from `sub`. Rejected: pushes an identity
concern into every server, needs per-server AWS permissions, re-solves a problem
the issuer already knows the answer to.

### Decision

**Ship Option A only.** Rationale:
- The access token is already wired end-to-end to the MCP handoff, so
  enrichment requires **zero** changes to the token-forwarding path.
- Option B solves the same problem with more plumbing across the Runtime
  inbound-auth boundary, for no additional capability.
- We only build the path we use. If a forker needs the ID-token path (their
  claims live in the ID token, or they can't use the enrichment hook), that is
  a documented, unimplemented extension point they can contribute back. Building
  it speculatively means maintaining an untested branch nothing exercises.

---

## Public-stack / forkability design

The mechanism must not hardcode an IdP, a claim name, or assume anyone needs it.

**Defaults make it inert.** A fork that doesn't need personalized MCP tools
configures nothing; the access token (with `sub`) is forwarded as it is today,
and no Lambda/trigger is created.

**Layered knobs:**

| Layer | Where | Default | Purpose |
|---|---|---|---|
| Identity claim | mcp-servers repo, `IDENTITY_CLAIM` env | `sub` | Which claim a server reads as the user id. Generic forks use `sub` and need nothing else. |
| Token enrichment | this repo, `mcpIdentity.tokenEnrichment` | `enabled: false` | Opt-in Pre-Token-Generation v2 Lambda that copies attributes → access-token claims. |

The enrichment mapping is `{claimName: sourceCognitoAttribute}` — generic. BSU's
`{ "employee_number": "custom:provider_sub" }` is *one configuration*, not the
canonical path.

### Config schema (`infrastructure/lib/config.ts`)

```typescript
export interface McpIdentityConfig {
  // Optional Pre-Token-Generation (v2) enrichment of the access token.
  // Off by default — no Lambda/trigger created when disabled.
  // Requires the Cognito Essentials/Plus feature plan.
  tokenEnrichment?: {
    enabled: boolean;
    // {claimName: sourceCognitoAttribute}. Present attributes are copied to
    // the named claim on the access token; missing attributes are skipped
    // (native users, forks without that attribute). No IdP assumed.
    accessTokenClaims?: { [claimName: string]: string };
  };
}

// AppConfig gains:
//   mcpIdentity: McpIdentityConfig;
```

`loadConfig()` (env > context > default):

```typescript
mcpIdentity: {
  tokenEnrichment: {
    enabled:
      parseBooleanEnv(process.env.CDK_MCP_TOKEN_ENRICHMENT_ENABLED)
      ?? scope.node.tryGetContext('mcpIdentity')?.tokenEnrichment?.enabled
      ?? false,
    accessTokenClaims:
      scope.node.tryGetContext('mcpIdentity')?.tokenEnrichment?.accessTokenClaims
      ?? {},
  },
},
```

`cdk.context.json` default (inert, documents the shape):

```jsonc
"mcpIdentity": {
  "tokenEnrichment": { "enabled": false, "accessTokenClaims": {} }
}
```

BSU deploy config:

```jsonc
"mcpIdentity": {
  "tokenEnrichment": {
    "enabled": true,
    "accessTokenClaims": { "employee_number": "custom:provider_sub" }
  }
}
```

---

## Implementation outline

1. **CDK construct** `lib/constructs/identity/token-enrichment-construct.ts`,
   instantiated only when `mcpIdentity.tokenEnrichment.enabled`:
   - Pre-Token-Generation **v2** Lambda; `accessTokenClaims` map passed as env
     (JSON). Generic asset — no per-fork code.
   - Attach as the pool's Pre-Token-Generation (v2 / `V2_0`) trigger.
2. **Lambda handler** (fail-safe): for each `{claim: attr}`, if the user
   attribute is present, add the claim to
   `claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride`.
   Missing attribute → skip. Any error → return event unchanged (never block
   login).
3. **mcp-servers repo**: personalized servers validate the Cognito access token
   (reuse `class-search/auth.py` pattern) and read `os.environ["IDENTITY_CLAIM"]`
   (default `sub`). For BSU, `IDENTITY_CLAIM=employee_number`.
4. **`student-myboisestate`**: replace Entra-ID JWKS validation with Cognito
   access-token validation; Function URL `AuthType: NONE`; register in the admin
   dashboard as `mcp_external` + `forward_auth_token: true`.

---

## Risks / caveats

- **Blast radius:** the trigger runs on every token issuance for the pool. The
  Lambda must be dependency-free, do no I/O, and fail open (return the event
  unchanged on any exception). Roll out to dev first.
- **Native vs federated users:** native Cognito users lack `custom:provider_sub`;
  the claim is simply omitted for them and personalized tools should report "no
  student identity" gracefully rather than error.
- **Stale-claim discipline:** only *stable* identity/coarse-role claims belong in
  the token. Fine-grained, volatile authorization (e.g. "is this instructor
  teaching section 12345 this term") must be a **runtime lookup in the tool**
  using the identity claim — never baked into the token.
- **Feature-plan dependency:** access-token customization needs Cognito
  ESSENTIALS/Plus. Confirmed for dev; verify the prod pool
  (`us-west-2_HPdmKV3f0`) before enabling in prod.

## Open questions

1. ~~Confirm the prod Cognito pool tier is ESSENTIALS/Plus.~~ **RESOLVED.** Prod
   and dev run the same CDK code, and the dev pool
   (`dev-boisestateai-v2-user-pool`) is on **Essentials**, which includes
   "Customize access token scopes and claims at runtime" (verified in console).
   The construct now pins the pool's `featurePlan` to `ESSENTIALS` so a fresh
   fork's pool supports the trigger before it attaches.
2. ~~Claim naming convention (`employee_number` vs a namespaced form).~~
   **RESOLVED: namespaced, full reverse-DNS form.** BSU uses
   `{"https://boisestate.edu/employee_number": "custom:provider_sub"}`; the
   public-stack inert example uses `https://example.com/employee_number`.
3. ~~Ship the enrichment construct in the public stack (disabled) or keep as a
   BSU overlay?~~ **RESOLVED: shipped in the public stack, disabled by default.**
   A fork configures nothing and gets zero resources; BSU flips it on with two
   GitHub Actions variables (`CDK_MCP_TOKEN_ENRICHMENT_ENABLED=true` +
   `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS='{"https://boisestate.edu/employee_number":"custom:provider_sub"}'`),
   leaving the committed `cdk.context.json` inert.

## Follow-on: mcp-servers repo (items 3–4, NOT in this repo)

The platform now *emits* the enriched claim; consuming it is the mcp-servers
repo's job and can proceed independently:

3. **Personalized servers validate the Cognito access token** (reuse the
   `class-search/auth.py` validation pattern) and read the identity from
   `os.environ["IDENTITY_CLAIM"]`, defaulting to `sub`. Generic forks leave the
   default and need no enrichment. BSU sets `IDENTITY_CLAIM` to the namespaced
   claim `https://boisestate.edu/employee_number`.
4. **`student-myboisestate`**: replace the Entra-ID JWKS validation with Cognito
   access-token validation; deploy the Function URL with `AuthType: NONE`; and
   register it in the admin dashboard as `mcp_external` with
   `forward_auth_token: true`. Native Cognito users (no `custom:provider_sub`)
   will lack the claim — the server must report "no student identity" gracefully
   rather than error.

**Discipline:** only stable identity / coarse-role claims belong in the token.
Volatile, fine-grained authorization (e.g. "is this instructor teaching section
12345 this term") must be a runtime lookup in the tool keyed on the identity
claim — never baked into the token.
