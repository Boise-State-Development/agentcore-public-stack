# MCP User Identity Forwarding — Implementation Guide

## Overview

Personalized MCP tools (e.g. `student-myboisestate`) need to identify the
logged-in user to scope data to them. The only token forwarded end-to-end to MCP
servers is the Cognito **access token**, which carries `sub` and little else.
This feature enriches the access token, in place, with configured identity claims
via a Cognito **Pre-Token-Generation v2** Lambda trigger — so the existing
forwarding path (SPA → app-api → inference-api → MCP server) needs no changes.

Design decision and rationale: `docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md`
(Option A). Shipped **disabled by default** in the public stack.

## Flow

```
User login / token refresh
   ↓
Cognito issues tokens
   ↓  (only if mcpIdentity.tokenEnrichment.enabled)
Pre-Token-Generation v2 Lambda (token-enrichment/handler.py)
   ↓  copies present user attributes → namespaced ACCESS-token claims
Access token (now carries e.g. https://boisestate.edu/employee_number)
   ↓  forwarded unchanged: SPA → app-api → inference-api → MCP server
MCP server reads IDENTITY_CLAIM (default `sub`; BSU: the namespaced claim)
```

## Two knobs

| Layer | Where | Default | Purpose |
|---|---|---|---|
| Identity claim | mcp-servers repo, `IDENTITY_CLAIM` env | `sub` | Which claim a server reads as the user id. Generic forks use `sub` and need nothing else. |
| Token enrichment | this repo, `mcpIdentity.tokenEnrichment` | `enabled: false` | Opt-in Pre-Token-Generation v2 Lambda that copies attributes → access-token claims. |

## Components (this repo)

### 1. Config (`infrastructure/lib/config.ts`)

`McpIdentityConfig.tokenEnrichment`:
- `enabled` — env `CDK_MCP_TOKEN_ENRICHMENT_ENABLED` > context
  `mcpIdentity.tokenEnrichment.enabled` > `false`.
- `accessTokenClaims` — a `{claimName: sourceCognitoAttribute}` map, from the
  JSON env var `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS` > context > `{}`. The JSON env
  var means a fork can enable the feature entirely through GitHub Actions
  variables while the committed `cdk.context.json` stays inert.

Claim names SHOULD be **namespaced** (full reverse-DNS form) to avoid colliding
with Cognito's reserved access-token claims.

### 2. Handler (`infrastructure/lambda-assets/token-enrichment/handler.py`)

Stdlib-only, dependency-free, no I/O. Reads `ACCESS_TOKEN_CLAIMS` (JSON), and for
each `{claim: attribute}` copies the attribute's value into
`response.claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride`.
Missing attributes are skipped (native Cognito users lack federated attributes).

**Fail-open contract:** the trigger runs on every token issuance for the whole
pool, so the handler wraps its body in a catch-all that returns the event
**unchanged** on any error. Worst case is "claim not added", never "login
blocked". Covered by `test_handler.py`.

### 3. Construct (`infrastructure/lib/constructs/identity/token-enrichment-construct.ts`)

Python 3.13 / ARM64 Lambda shipping its **real** handler via
`lambda.Code.fromAsset` (no bootstrap placeholder — it's generic, tiny, and
rarely changes, so it rides the normal `platform.yml` CDK deploy path). Attaches
as the pool's Pre-Token-Generation v2 trigger via
`userPool.addTrigger(PRE_TOKEN_GENERATION_CONFIG, fn, LambdaVersion.V2_0)`, which
also auto-grants Cognito invoke permission. Instantiated by `platform-stack.ts`
only when `config.mcpIdentity.tokenEnrichment.enabled`.

`CognitoConstruct` pins the pool `featurePlan` to `ESSENTIALS` (required for
access-token customization; a no-op on already-Essentials pools).

## Enabling it (BSU / any deployer)

Disabled by default — a fork that does nothing gets no Lambda and no trigger.
To enable, set two GitHub Actions **variables** on the environment used by
`platform.yml`:

```
CDK_MCP_TOKEN_ENRICHMENT_ENABLED = true
CDK_MCP_TOKEN_ENRICHMENT_CLAIMS  = {"https://boisestate.edu/employee_number":"custom:provider_sub"}
```

Then run the platform (CDK) deploy. Flipping the flag on creates the Lambda +
trigger and attaches it to the existing pool in place; flipping it off removes
them cleanly. (This is an infrastructure change — it goes through `platform.yml`,
not the fast `backend.yml` code-deploy path.)

**Requirement:** the Cognito pool must be on the Essentials (or Plus) feature
plan. The construct pins Essentials, and existing pools already are.

## Follow-on (mcp-servers repo — not in this repo)

The platform now emits the claim; servers consume it:

1. Personalized servers validate the Cognito access token (reuse the
   `class-search/auth.py` pattern) and read `os.environ["IDENTITY_CLAIM"]`
   (default `sub`; BSU sets `https://boisestate.edu/employee_number`).
2. `student-myboisestate` swaps Entra-ID JWKS validation for Cognito
   access-token validation, deploys its Function URL with `AuthType: NONE`, and
   registers as `mcp_external` + `forward_auth_token: true` in the admin
   dashboard. It must degrade gracefully to "no student identity" for native
   users that lack `custom:provider_sub`.

**Stale-claim discipline:** only stable identity / coarse-role claims belong in
the token. Volatile fine-grained authorization must be a runtime lookup in the
tool keyed on the identity claim — never baked into the token.

## Tests

- `infrastructure/lambda-assets/token-enrichment/test_handler.py` — handler
  enrichment + fail-open (14 cases, `uv run pytest`).
- `infrastructure/test/config.test.ts` — config precedence (env > context >
  default) for the flag and the claim map, including malformed-JSON fall-through.
- `infrastructure/test/token-enrichment.test.ts` — construct synth: Lambda,
  v2 trigger, invoke permission, empty-map no-op, and the disabled no-resources case.
- `infrastructure/test/platform-stack.test.ts` — stack-level wiring: disabled
  default emits no trigger; enabled emits the trigger + claim-map Lambda.
