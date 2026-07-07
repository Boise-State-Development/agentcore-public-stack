# Managed-Harness spike — findings (three gating questions answered)

**Date:** 2026-07-06 · **Status:** spike complete (analysis) · **Brief:** `docs/kaizen/scoping/2026-07-06-managed-harness-build-vs-adopt.md`
**Method:** answered from (a) the now-GA AWS AgentCore **managed Harness** docs — authoritative on the product's capabilities, which are past training cutoff — and (b) our own code, which is authoritative on what the headless lane actually requires. Cross-checked against the F1 entrypoint spike (`docs/specs/harness-entrypoint-spike-findings.md`, proven live in dev-ai 2026-07-05).
**Not done:** a live `create-harness` / `InvokeHarness` deployment in dev-ai. Two of the three questions are fully closed by docs + already-proven code; the third has exactly one residual that *needs* a live probe, flagged below. That probe — not a full harness stand-up — is the only thing gating a decision.

---

## Verdict at a glance

| # | Question | Verdict |
|---|---|---|
| 1 | RBAC → `allowedTools` (+ Cedar), per-invoke, without hook enforcement | **Qualified YES** — set membership reduces cleanly (we already snapshot it statically); three non-membership gates must relocate, none blocking for headless |
| 2 | Per-user connector tokens via OAuth-inbound + Identity vault, as schedule owner | **YES on the mechanism** (it *is* our primitive). Residual **probed live 2026-07-06 → GO-with-boundary:** `customParameters` pinning is present & honored (config persists verbatim; forwarded into the same `GetResourceOauth2Token` call), but the managed **Gateway** 3LO exchange is blocked by a `ResourceOauth2ReturnUrl` wiring gap → keep 3LO connectors on our own `get_token_for_user`. See **Q2 probe result** below. |
| 3 | Acceptable to lose MCP Apps UI + our SSE contract on headless | **YES** — the losses land entirely on interactive affordances a scheduled run has no consumer for |

**Net:** all three clear for the headless/scheduled lane. Green-light a scoped `InvokeHarness` prototype whose one job is to close the Q2 residual; everything else is already answered.

---

## Q1 — RBAC → `allowedTools` (+ Gateway Cedar)

**Does our per-user RBAC-resolved tool set reduce cleanly to Harness `allowedTools` globs per invocation, without hook-based enforcement?**

### Yes for the part that matters — and we already produce the input

Two facts make this cleaner than the brief feared:

1. **`allowedTools` is settable per-invocation.** The Harness accepts `tools` / `allowedTools` overrides on a single `InvokeHarness` call (`agentcore invoke --tools …`; `client.invoke_harness(tools=…)`), not just at harness-create. So a dispatcher can compute a per-owner, per-schedule tool scope and pass it at invoke time. Supported globs cover our id shapes: `@server/tool`, `@server/glob` (`@git/read_*`), `@*/tool` across servers, plain builtin names.
2. **We already resolve the set statically, at the right boundary.** RBAC resolution is a pure function of static attributes (`AppRoleService.resolve_user_permissions` → union of role `tools`, wildcard, quota tier) and it's applied at the **app-api** security boundary, not inside the agent loop: `filter_requested_tools()` (`apis/shared/rbac/service.py:217`, "narrow-never-grant"). Schedules **already snapshot the narrowed `enabled_tools`** at creation (`apis/app_api/schedules/routes.py:75–98`). The dispatcher therefore already holds the exact static allow-list; emitting `allowedTools` globs from it is a mechanical id→glob translation (`gateway_<target>___<tool>` → `@<gateway>/<tool>`; `linear::*` → `@linear/*`).

Gateway **Cedar policies** then cover the conditional / argument-level gating that globs can't express ("who may call which tool, under which conditions, with which arguments") — a strict *superset* of what our up-front list filter does today.

### What does NOT reduce to `allowedTools` — and where each goes instead

The managed Harness has **no hook seam** (`Hooks ❌`). Three of our enforcement points are call-time hooks, not list membership. None blocks headless, but each must relocate:

| Gate (our hook) | Reduces to `allowedTools`? | Disposition on the headless lane |
|---|---|---|
| RBAC tool-fold | **Yes** — pure static set | Emit as `allowedTools` glob per invoke (dispatcher already has the snapshot) |
| OAuth-consent (`BeforeToolCallEvent`) | No — token-vault check at call time | **Relocated, not lost:** the Harness's OAuth-inbound + Identity outbound *is* the on-behalf-of exchange (see Q2). Happy path is automatic; a missing/unconsented token fails the exchange → map to our `paused_reauth` / `oauth_required` status. A headless run can't pop consent interactively anyway — same constraint as today. |
| Tool-approval HITL (`MCPExternalApprovalHook`) | No — needs a human decision | **Exclude approval-gated tools from the schedule snapshot** (least-surprise; matches punch-list item 7 in the F1 findings). Harness *does* offer `inline_function` (`stopReason: tool_use` → client returns result) if we ever want async-approval schedules, but default = exclude. |
| Quota / cost | No — per-user runtime state, not per-tool | **Move to the dispatcher:** a pre-invoke quota gate (our governance floor F6a `check_input` seam already exists) + post-run cost rollup from the Harness `metadata`/usage output. Harness "execution limits" are per-harness/per-invoke config — coarser than our per-user tiers, so we keep owning quota. |

> ⚠️ **Trust-boundary note the docs are emphatic about:** *all* `InvokeHarness` input is trusted — `allowedTools` "scopes LLM tool selection only," it is **not** a security boundary against the caller, and it does **not** gate `InvokeAgentRuntimeCommand` (direct command exec, separate IAM action — simply don't grant `bedrock-agentcore:InvokeAgentRuntimeCommand`). This is fine for us: on the headless lane **we are the caller** and we compute `allowedTools` from RBAC before invoking. The security boundary stays where it already is — the app-api dispatcher — exactly as `/runs/now` and `/schedules` enforce it today. We must not forward untrusted end-user input straight into `InvokeHarness` (`skills`/`model`/`additionalParams` are all caller-overridable per-invoke).

**Q1 conclusion:** the RBAC *set* reduces cleanly and we already compute it in the right place. Cedar strengthens arg-level gating. The three non-membership gates are either automatic (OAuth via Identity), an acceptable exclusion (approval), or a dispatcher responsibility we already own (quota/cost). **Clears.**

---

## Q2 — Per-user connector tokens (OAuth-inbound + AgentCore Identity, as schedule owner)

**Do our vaulted 3LO tokens resolve through Harness OAuth-inbound + Identity on-behalf-of, acting as the owner, exactly as `run_agent_headless` does today?**

### Yes — it is literally the same primitive, on an authorizer we already run

The AWS security doc is unambiguous:

- **OAuth-inbound path threads the end-user identity** through the agent "so downstream tools can call APIs with scoped user credentials instead of a shared service account." Gateway tools take an `outboundAuth.oauth` credential provider + scopes and the vault performs the on-behalf-of exchange.
- **SigV4 does NOT propagate per-user identity** — "per-user credential scoping … only available when callers authenticate with a Bearer JWT via the OAuth inbound path. SigV4 support for per-user identity is planned for a future release." So the harness **must** be configured OAuth-inbound. That aligns exactly with what we already do.

Our side already matches, on three counts:

1. **Same authorizer, verbatim.** Our Runtime is configured with `customJwtAuthorizer { discoveryUrl: <Cognito>, allowedClients: [BFF app client] }` (`infrastructure/lib/constructs/inference-api/inference-agentcore-construct.ts:275`). The Harness `customJWTAuthorizer` takes the identical shape. A 1:1 port.
2. **Same minted owner token.** The F1 spike already mints a real Cognito **access token for the owning user** (refresh-token grant from the BFF session / headless-grant record) and proved it threads three layers deep as the user. That is exactly the Bearer the Harness OAuth-inbound path wants; the JWT's `sub` = owner, so the run acts *as* the owner.
3. **Same outbound exchange.** `AgentCoreIdentityService.get_token_for_user` (`apis/shared/oauth/agentcore_identity.py:168`) already does `USER_FEDERATION` / `GetWorkloadAccessTokenForUserId` → `GetResourceOauth2Token`, user-scoped. The F1 spike proved this unattended (minted the owner's google-drive token from the vault and listed real Drive files). The managed Harness's "AgentCore Identity outbound" is this same call, moved behind Gateway config.

### The one residual that needs a live probe — `customParameters` pinning

Our vault has a documented gotcha ([[project_agentcore_custom_parameters_vault_key]]): a token retrieval must send the **same `customParameters`** the consent flow used, or the vault key misses and it falsely reports consent-required. In our own code we pass `custom_parameters` explicitly into `get_token`. In the managed Harness, the outbound OAuth is **configured on the Gateway tool** (`credentialProviderName` + `scopes`) and the Harness/Gateway performs the exchange — so we lose direct call-site control over `customParameters`.

- Providers with **no** `customParameters` (the common case — plain `scopes`): expected clean.
- Providers that **need** `customParameters` to hit the right vault key: **open risk.** Either Gateway outbound-auth config exposes a way to pin them, or those providers can't be driven through a Harness-managed exchange and stay on our own `get_token_for_user` path.

**This is the single thing worth a live `InvokeHarness` probe** — configure one Gateway OAuth tool for a `customParameters`-sensitive provider and confirm the owner's vaulted token resolves. Everything else in Q2 is already proven.

**Q2 conclusion:** mechanism confirmed and already ours; must configure OAuth-inbound (SigV4 can't do per-user); one live-probe on `customParameters` pinning before trusting it for vault-key-sensitive connectors. **Clears, with a named probe.**

---

## Q3 — Acceptable losses on the headless path (MCP Apps UI + our SSE contract)

**Hypothesis: yes — a scheduled run delivers a session, not an interactive App frame.** Confirmed.

- **MCP Apps UI (SEP-1865)** is an interactive-frame concept — it only means anything when a human is watching the stream live and the SPA can mount the App iframe. A scheduled run has **no live viewer at emission time**. A tool that would emit a `ui_resource` still executes as a plain tool; it just renders no frame. Zero functional loss on headless.
- **Our SSE event vocabulary** is consumed **server-side** on this lane, never by the SPA. Our runner already drains the stream into a structured `RunResult` (`apis/shared/harness/sse.py` `InvocationStreamAccumulator`). The managed Harness returns a Bedrock **Converse-shaped** stream (`contentBlockStart/Delta`, `toolUse`, `stopReason`, `metadata`). Adopting it swaps our accumulator for a Converse-stream accumulator producing the **same `RunResult`**. The SPA never sees either stream — it loads the **delivered session** (persisted messages + title + metadata row), which the F1 spike proved the runtime turn already materializes. So the SSE-contract loss is invisible on the headless lane.

Two affordances to carry over deliberately (both already in the F1 design):

- **`oauth_required` as a first-class status** — a headless run can't pop consent; the scheduler pauses (`paused_reauth`) and surfaces the URL. Must confirm the Harness/Gateway exchange *fails legibly* (returns/streams something we can map) rather than silently erroring when a vaulted token is missing. Pairs with the Q2 probe.
- **Tool-approval** — no approver on a schedule → excluded (Q1), not a streaming concern.

**Q3 conclusion:** the losses are real but land entirely on interactive affordances a scheduled run has no consumer for. **Clears.**

---

## Recommendation

**Green-light a narrowly scoped `InvokeHarness` prototype** whose sole gating purpose is to close the **Q2 `customParameters` residual** (one Gateway OAuth tool, one `customParameters`-sensitive provider, owner-minted Bearer, confirm the vaulted token resolves and that a *missing* token fails legibly for `paused_reauth`). Q1 and Q3 are answered; Q1's non-membership gates have clear homes (dispatcher for quota/cost, exclusion for approval, Identity for consent).

If the probe clears, the payoff is the brief's thesis intact: **managed memory (reads in cloud → dents F5) + immutable versions/endpoints + `InvokeHarness` Step Functions composition on the proactive lane, without touching interactive chat**, with `agentcore export harness` as the low-lock-in exit. Keep the interactive `inference-api` stack exactly where it is — it depends on the hook seam, custom loop, MCP Apps hosting, and SSE contract the managed Harness forbids.

### Refs
- AWS: `devguide/harness.html`, `harness-tools.html` (`allowedTools` globs, per-invoke override, inline_function, trust boundary), `harness-security.html` (OAuth-inbound vs SigV4 per-user identity, Cedar on Gateway, outbound OAuth2 credential provider).
- Internal: `docs/specs/harness-entrypoint-spike-findings.md` (F1, proven live), `apis/shared/oauth/agentcore_identity.py:168`, `apis/shared/rbac/service.py:217`, `apis/app_api/schedules/routes.py:75`, `infrastructure/lib/constructs/inference-api/inference-agentcore-construct.ts:275`.

---

## Q2 probe result — `customParameters` pinning (live, dev-ai 2026-07-06)

**Verdict: GO-with-boundary.** The pinning mechanism the residual asked about **exists and is honored** — confirmed live. But a *second*, newly-discovered boundary means vault-key-sensitive 3LO connectors should keep running through **our own `get_token_for_user` call-site path** (already proven by F1), not the Harness-managed Gateway exchange, until AWS's return-URL wiring gap is resolved. This *sharpens and reinforces* the original recommendation rather than changing it.

**Method:** Product is real & GA (**AgentCore managed Harness, GA 2026-06-18, AWS NY Summit** — past training cutoff, so validated live). Called the real APIs via `boto3 1.43.9` (the backend `agentcore` extra; the system `aws` CLI 2.22.12 predates the service). Reused infra to minimise footprint: the **runtime execution role** (`dev-boisestateai-v2-agentcore-runtime-role`, trust already allows `bedrock-agentcore.amazonaws.com`), the **existing dev Gateway**, the real **`google-drive` Identity provider**, and a **purpose-minted headless-grant** owner (`18419330-…`, the F1-proven holder of a consented `google-drive` vault token). No new IAM role, no gateway-target changes.

### What was confirmed LIVE

1. **`CreateHarness` accepts our exact `customJWTAuthorizer`** — a 1:1 port of `inference-agentcore-construct.ts:275` (`discoveryUrl` = Cognito pool, `allowedClients=[BFF client]`). Harness reached `READY`. (Aside: managed memory is provisioned **by default** — a `managedMemoryConfiguration` appears unbidden; relevant to the brief's F5 "reads in cloud" thesis. Defaults observed: model `global.anthropic.claude-sonnet-4-6`, `allowedTools:["*"]`, sliding-window-150 truncation, `idleRuntimeSessionTimeout=900s`, `maxLifetime=28800s`, `maxIterations=75`.)
2. **`outboundAuth.oauth.customParameters` is a first-class, honored config field.** botocore contract exposes it (map, required-alongside `providerArn`+`scopes`, at **both** create-time and per-invoke `tools`). `GetHarness` **round-trips it verbatim** — tested with the 2-key `{prompt,access_type}` and the 3-key `{hd,prompt,access_type}` maps, persisted exactly, never dropped. **→ We CAN pin the same `customParameters` our consent flow uses.** This is the core residual question, answered YES.
3. **OAuth-inbound works end-to-end.** Minted the owner's Cognito access token (F1 `CognitoRefreshBearerAuth` refresh-grant), `POST /harnesses/invoke` with `Authorization: Bearer …` → **HTTP 200, harness ran as the owner.**
4. **The Harness-managed exchange IS our primitive.** When the agent loads the gateway OAuth tool, the runtime calls **`GetResourceOauth2Token`** — the *exact* vault API `apis/shared/oauth/agentcore_identity.py:168` `get_token_for_user` and F1 already use — forwarding the configured `outboundAuth`. So the on-behalf-of exchange is the same call, relocated behind Gateway config (Q2's mechanism claim, confirmed at the wire).
5. **Failures surface LEGIBLY (Q3 step 5 / `paused_reauth`).** A failed exchange arrives as a **typed `runtimeClientError` exception event** in the InvokeHarness stream carrying structured JSON (`{"message": "… GetResourceOauth2Token … <reason>"}`) — mappable to our `oauth_required` → scheduler `paused_reauth`. Not a silent error.

### The boundary discovered live (why not a clean GO)

I could **not** observe a clean *positive* token-resolution through the managed Gateway exchange. Every owner-scoped 3LO (`AUTHORIZATION_CODE`) attempt failed at gateway-tool init with:

> `ValidationException … GetResourceOauth2Token … You must provide a ResourceOauth2ReturnUrl to proceed with this flow`

I supplied the return URL through **every** documented channel and the error persisted identically:
- per-invoke `tools[].outboundAuth.oauth.defaultReturnUrl`,
- create-time (persisted) `defaultReturnUrl` (confirmed via `GetHarness` round-trip),
- the `OAuth2CallbackUrl` request header (the channel Runtime uses),
- registering the URL as `AllowedResourceOauth2ReturnUrl` on the harness's auto-created workload identity via `UpdateWorkloadIdentity` (the mechanism `oauth2-authorization-url-session-binding.html` prescribes).

**Conclusion:** in this GA build the managed **Gateway** 3LO exchange does not source the `resourceOauth2ReturnUrl` from `defaultReturnUrl`/header/workload registration for the gateway-MCP-client-init flow — an undocumented-required-channel or a genuine gap. Because the flow errored *before* any cache lookup, I could not run the intended discriminator (correct `{hd,prompt,access_type}` → resolves vs. `hd`-omitted → consent-required). A related **unknown remains unreached past this blocker:** cross-workload token visibility — F1's token was consented under our **platform** workload identity, whereas the harness runs under its **own** auto-created workload identity; whether a platform-consented token is even visible to the harness workload (absent `scope-credential-provider-access`) is untested.

### Recommendation

- **GO** to adopt the managed Harness on the **headless/scheduled lane** (Q1+Q3 already clear; this probe adds live proof of the customJWTAuthorizer port, OAuth-inbound, and legible failure surfacing).
- **Boundary:** drive `customParameters`-sensitive (and, conservatively, *all* 3LO) connectors through **our own `get_token_for_user`** inside the run — the path F1 already proved unattended — **not** the Harness-managed Gateway `outboundAuth.oauth` exchange, until (a) the `ResourceOauth2ReturnUrl` wiring is resolved with AWS and (b) cross-workload token visibility is confirmed. Zero loss vs. today: we keep the call-site control the vault-key gotcha ([[project_agentcore_custom_parameters_vault_key]]) requires. Plain-scopes / non-3LO providers are unaffected.
- **Static-pin sub-finding stands:** our `customParameters` are admin-static per-connector (`custom_parameters_for`, `oauth/models.py`), so a static Harness config *can* carry byte-identical values — the pinning is expressible; only the exchange plumbing blocks it today.

**Follow-ups worth a short AWS-support / re-probe pass:** ① correct channel for `ResourceOauth2ReturnUrl` on a managed Gateway 3LO exchange (or confirm the gap); ② harness-workload vs platform-workload token visibility (`scope-credential-provider-access`). **Pricing:** no separate harness charge — you pay only for the underlying AgentCore capabilities used (`harness.html`); note managed memory is on by default (an extra Memory cost dimension per harness).

**Teardown:** probe harness `q2probe_cparams` deleted (its runtime + managed memory + auto-created workload identity `harness_q2probe_cparams-*` are removed with it); the reused runtime role, dev Gateway, and its `arxiv`/`policy-search` targets were never modified.

---

## `resourceOauth2ReturnUrl` — the parameter shape (added 2026-07-07)

The blocker above was empirical ("supplied it four ways, all failed"). The API doc for the field now gives the **mechanism-level** reason, and it *confirms* the boundary decision rather than reopening it.

**Shape (from the AWS API reference):**

> **`resourceOauth2ReturnUrl`** — The callback URL to redirect to after the OAuth 2.0 token retrieval is complete. **This URL must be one of the provided URLs configured for the workload identity.**
> Type: String · Length 1–2048 · Pattern `\w+:(\/?\/?)[^\s]+` · **Required: No**

Three things fall out of this, and each is decision-relevant:

1. **It is a direct request parameter of `GetResourceOauth2Token` itself** — not a harness/gateway config field. So on **our own** `get_token_for_user` call-site (`apis/shared/oauth/agentcore_identity.py:168`) we *can* pass it; on the **Harness-managed Gateway** path the Gateway is the caller of `GetResourceOauth2Token`, and — as the probe found — it exposes **no seam to inject this parameter** into that internal call (`defaultReturnUrl`, the `OAuth2CallbackUrl` header, and workload-identity registration all failed to thread through). This upgrades the boundary from "undocumented channel" to a **structural** one: the value belongs on a call we don't make on the managed path. **→ strengthens "keep 3LO on our own path."**
2. **`Required: No`** — it is only needed when the flow must actually redirect the user for a *fresh* `AUTHORIZATION_CODE` consent. An **already-consented / cached** vault token resolves without it. This is exactly why F1's pre-consented `google-drive` token worked unattended and why plain-scopes/2LO providers are unaffected — the return URL matters *only* at initial consent, which on our lane happens through the SPA/BFF, not inside the run.
3. **"must be one of the provided URLs configured for the workload identity"** — the allow-list linkage the probe attempted (`AllowedResourceOauth2ReturnUrl` via `UpdateWorkloadIdentity`) is the *right* registration mechanism, but it must be registered on **the workload identity that actually performs the exchange**. On the managed path that is the harness's **own** auto-created `harness_<agentName>-*` identity — which ties directly into the still-unreached **cross-workload visibility** residual (a token consented under our *platform* workload identity is registered against a *different* identity than the one the harness exchange runs under).

### harness-security.html cross-check (2026-07-07)

Reading the harness **security** page confirmed two supporting facts and surfaced one new documentary signal:

- **SigV4 cannot carry per-user identity — verbatim:** *"When callers authenticate with SigV4 (AWS IAM), the harness does not propagate per-user identity into downstream tool calls … user-scoped OAuth token storage and on-behalf-of token exchange … only available when callers authenticate with a Bearer JWT via the OAuth inbound path. SigV4 support for per-user identity is planned for a future release."* Confirms the OAuth-inbound requirement (Q2) in AWS's own words.
- **The intended pattern is on-behalf-of *exchange*, not consent *origination*.** The page describes threading an inbound user JWT into a downstream token exchange; it documents **no** interactive-consent origination from inside a run. That is consistent with `Required: No` above — a headless run is expected to exchange an *already-consented* token, not to start a redirect. Our decision (originate consent on our own SPA/BFF path, let the harness only exchange) is *aligned with the design*, not a workaround.
- **New documentary signal — the harness runs under its own workload identity.** The page's "OAuth2 credential provider (OAuth-protected Gateway)" IAM policy scopes `bedrock-agentcore:GetResourceOauth2Token` to `…/workload-identity/harness_<agentName>-*`. This is written confirmation of the cross-workload residual: the harness's exchange authority is namespaced to its **own** identity, distinct from our platform workload identity where existing tokens were consented. Whether a platform-consented token is visible across that boundary (via `scope-credential-provider-access`) remains the **one** thing to test before any managed-Gateway 3LO adoption.
- **`resourceOauth2ReturnUrl` appears nowhere on the security page** — no 3LO return-URL / callback documentation at all. Consistent with the probe's read that the correct channel on the managed-Gateway path is undocumented (or absent) in this GA build.

**Net:** the return-URL shape and the security-page cross-check **do not change the GO-with-boundary verdict** — they re-label the open question. The blocker is best understood as *structural* (the return-URL param lives on a call the managed path makes internally, against the harness's own workload identity), and the sharpest next test is **cross-workload token visibility**, not chasing the return-URL channel. Follow-up ① above is refined accordingly: confirm with AWS whether a managed-Gateway 3LO exchange can ever accept a caller-supplied `resourceOauth2ReturnUrl`; if not, the "our own `get_token_for_user`" boundary is permanent-by-design, not a stopgap.
