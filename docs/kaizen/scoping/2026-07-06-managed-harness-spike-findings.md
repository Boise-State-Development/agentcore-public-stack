# Managed-Harness spike — findings (three gating questions answered)

**Date:** 2026-07-06 · **Status:** spike complete (analysis) · **Brief:** `docs/kaizen/scoping/2026-07-06-managed-harness-build-vs-adopt.md`
**Method:** answered from (a) the now-GA AWS AgentCore **managed Harness** docs — authoritative on the product's capabilities, which are past training cutoff — and (b) our own code, which is authoritative on what the headless lane actually requires. Cross-checked against the F1 entrypoint spike (`docs/specs/harness-entrypoint-spike-findings.md`, proven live in dev-ai 2026-07-05).
**Not done:** a live `create-harness` / `InvokeHarness` deployment in dev-ai. Two of the three questions are fully closed by docs + already-proven code; the third has exactly one residual that *needs* a live probe, flagged below. That probe — not a full harness stand-up — is the only thing gating a decision.

---

## Verdict at a glance

| # | Question | Verdict |
|---|---|---|
| 1 | RBAC → `allowedTools` (+ Cedar), per-invoke, without hook enforcement | **Qualified YES** — set membership reduces cleanly (we already snapshot it statically); three non-membership gates must relocate, none blocking for headless |
| 2 | Per-user connector tokens via OAuth-inbound + Identity vault, as schedule owner | **YES on the mechanism** (it *is* our primitive) — one residual live-probe: `customParameters` pinning on the Gateway outbound exchange |
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
