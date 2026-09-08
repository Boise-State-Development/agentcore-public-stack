# WebMCP host: the SPA as an agent-callable surface

**Status:** spec / not built. Supersedes the "browser agent first" framing.
**Refs:** SEP-1865 MCP Apps host work (`docs/kaizen/scoping/mcp-apps-host-renderer.md`),
`docs/specs/tool-search-token-bloat-strategy.md`, `docs/specs/mid-turn-steering.md`
**External:** [Angular WebMCP](https://angular.dev/ai/webmcp) (experimental),
W3C WebMCP CG draft (`document.modelContext`, formerly `navigator.modelContext`)

## Problem

The ask was "build a browser agent so we can leverage WebMCP later." Examining
what WebMCP actually is inverts that ordering.

WebMCP lets a **page** declare tools — `{name, description, inputSchema, execute}` —
that an agent can call directly instead of driving pixels and DOM. That yields
two entirely different products, and only one of them is worth building now:

**Outbound** — our agent drives *other people's* websites through a headless
cloud browser. WebMCP helps only where the target site has adopted it. Chrome's
origin trial opened May 2026; external adoption today is effectively zero. So an
outbound browser agent has to justify itself on DOM driving alone, where it is an
expensive commodity: per-session billing on top of model tokens, unbounded
per-step payloads (accessibility trees, screenshots, page text), bot-blocking
(AWS's own guidance is to avoid Google because cloud browser IPs get CAPTCHA'd),
and no story for anything behind a login.

**Inbound** — *our* platform becomes agent-callable. Our own agent drives the UI
the user is looking at, inside the user's authenticated session. Later, external
agents (Claude for Chrome, Gemini in Chrome) can drive Boise State's platform
through the same declarations.

Inbound pays off immediately, depends on nobody else's adoption curve, and is the
concrete form of the agentic-enabler thesis. It also needs **no AgentCore
Browser, no Playwright, and no headless anything.**

The cost tenet points the same way. Outbound browsing produces unbounded per-turn
payloads by construction. An inbound tool call returns a bounded structured
result — the page already knows its own state, so there is nothing to scrape.
Inbound is strictly the cheaper agentic surface.

This spec covers inbound. Outbound is deferred to its own decision (see
[Appendix B](#appendix-b--what-this-defers)).

## What already exists

We ship most of a WebMCP host today. It is simply pointed at iframes.

**A bidirectional JSON-RPC bus over postMessage.**
`frontend/ai.client/src/app/session/services/mcp-apps/mcp-app-bridge.ts` is the
host half. It is not one-directional: host→View requests with promise
correlation are already implemented —

```
mcp-app-bridge.ts:186   /** Pending host→View requests awaiting a JSON-RPC response, by id. */
mcp-app-bridge.ts:648   private sendRequest(method: string, params: unknown): Promise<unknown>
mcp-app-bridge.ts:222   this.sendRequest(M_RESOURCE_TEARDOWN, { reason })
```

so "the host asks the page to do something and awaits a reply" is a shape the
bridge already implements and ships. The protocol constants, including
`tools/call`, live in `mcp-app-protocol.ts:75`.

**Page→agent tool calls with full auth and consent.**
`apis/inference_api/chat/app_tool_dispatch.py` runs an App-initiated `tools/call`
**without a model turn**: it rebuilds the conversation's agent via `get_agent` so
the MCP client session, OAuth token cache, SigV4 signing and consent hook are
wired exactly as for a model-driven call, re-checks the tool's
`_meta.ui.visibility`, and publishes synthesized `tool_use` / `tool_result`
events to the per-session broker so the live conversation renders the card.
Entry point is app-api's `POST /mcp-apps/proxy-call`
(`apis/app_api/mcp_apps/routes.py:65`).

**A prompt-cache-safe way to get page state into the model.**
`apis/inference_api/chat/app_context_dispatch.py:79` stashes app-pushed context
on the cached agent's Strands `agent.state`, keyed by resource URI, and
`merge_and_clear_pending_context` (`:155`) renders it into **that turn's prompt only** —
explicitly kept out of persisted history and the cached system prefix. Its module
docstring is the pattern this spec reuses wholesale.

**A user-consent path for page-originated actions.**
`mcp-app-consent.service.ts` + `mcp-app-consent-prompt.component.ts`, gated
host-side before the host acts.

**A proven side channel into a running turn.** The single-flight lease row
carries owner-scoped signals to whichever container is streaming:
`request_session_cancel` (`apis/shared/sessions/session_lease.py:305`),
`request_session_steer` (`:420`), `peek_steer_queue` (`:576`),
`clear_steer_entry` (`:672`). Mid-turn steering proved the pattern end to end.

**Idle browser infrastructure.** `AgentCoreBrowserConstruct` already deploys a
`CfnBrowserCustom` with IAM
(`infrastructure/lib/constructs/inference-api/inference-agentcore-construct.ts:220`)
and `BROWSER_ID` in the runtime env (`:352`). Nothing consumes it — the only
reader is a startup log line (`apis/inference_api/main.py:61`). That is a fact
about optionality, not a reason to build outbound now.

### What does *not* exist

- Any notion of the **top-level SPA** as a tool provider. Everything above binds
  a tool set to a sandboxed iframe with a `ui://` resource URI and a proxy origin.
- The **agent→page** call direction as a *product* path. The bridge supports
  host→View requests, but no agent-side tool routes to one, and the transport
  from the inference container out to the user's browser mid-turn is unbuilt.
- Any SPA-side tool registry. Angular components expose nothing today.

## Non-goals

- Outbound browsing / AgentCore Browser. Deferred, separately decided.
- Exposing SPA tools to third-party agents in v1. The `document.modelContext`
  projection is PR-4 and is deliberately last — it is the part that depends on an
  unstable external spec.
- Replacing app-api endpoints. Page tools act on the *view*; they are not a
  general API surface and must not become one.

## Design

### D1 — One registry, two consumers

Both relevant specs are moving. Angular's API is `provideExperimentalWebMcpTools`,
documented as subject to change outside major versions. The W3C surface renamed
its global (`navigator.modelContext` → `document.modelContext`) within months,
and `provideContext()`/`clearContext()` were replaced by
`registerTool()`/`unregisterTool()`.

So **do not couple our host to either shape.** Define our own declaration
primitive and make the standards adapters over it:

```
                      ┌────────────────────────────┐
   Angular feature ──▶ │  SpaToolRegistry (ours)    │
   declares a tool     │  name, description,        │
                      │  inputSchema, execute,     │
                      │  scope, requiresConsent     │
                      └─────────┬──────────┬────────┘
                                │          │
                    (PR-1) ─────┘          └───── (PR-4)
                 our agent, over the        document.modelContext
                 existing bridge            projection for
                                            third-party agents
```

Consequences:

- We get the inbound value **before** WebMCP stabilizes, because our own agent
  never goes through `modelContext` — it goes through our bus.
- Spec churn is a one-file adapter change.
- If the page already carries a native or polyfilled implementation, the
  projection **wraps** it rather than clobbering it.

A tool declaration is route-scoped by default: registered on activation,
unregistered on teardown. That is deliberate — the available tool set is a
function of what the user is looking at.

### D2 — Transport: how the agent calls a tool in the user's tab

The agent runs in the inference-api container; the tool executes in the user's
browser. Both legs already exist as proven transports:

```
  agent loop                                              user's browser
      │                                                         │
      │ 1. model calls page_action(...)                         │
      │ 2. emit SSE `page_tool_call` {callId, name, arguments}   │
      ├────────────── live turn's SSE stream ──────────────────▶ │
      │                                                         │ 3. SpaToolRegistry
      │                                                         │    executes; consent
      │                                                         │    prompt if required
      │ 5. tool coroutine awaits, polling the lease row         │
      │ ◀───────── POST /sessions/{id}/page-tool-result ─────────┤ 4.
      │            (app-api → lease row, owner-scoped)           │
      │ 6. resolve, return bounded result to the model           │
```

**Out** rides the turn's own SSE stream — mid-turn emission is already proven by
`steering_applied` and `model_retry`.

**In** rides the single-flight lease row, exactly as `steerQueue` does: app-api
stamps an owner-scoped `pageToolResults` entry; the blocked tool coroutine polls
on the same cadence as the existing heartbeat and clears its entry by correlation
id. This is cross-container safe by construction — the arming request may land on
a different container than the one streaming, which is precisely the problem the
lease row was built to solve.

**Why not interrupt/resume.** The OAuth consent path already does "pause a turn,
get something from the user, resume." It would work, but each call costs a full
turn restart and re-establishment against the prefix. A blocking tool with a side
channel costs nothing extra and keeps the turn append-only.

**Bounding.** A page-tool call must not hang a turn:
- hard timeout (proposed 30s) → the tool returns a structured timeout result, the
  model sees it and moves on. Never raise into the agent loop.
- no live SPA subscriber for the session → immediate "page not available" result.
- max page-tool calls per turn (proposed 10) → guards a loop.
- a cancelled turn resolves every pending call immediately.

### D3 — Prompt-cache: one stable tool, not N churning ones

This is the constraint that shapes the whole design. All tool sources converge at
`BaseAgent._build_filtered_tools()` (`backend/src/agents/main_agent/base_agent.py:448`)
and are serialized into the Bedrock `toolConfig` **every turn**. `toolConfig` is
part of the cached prefix.

Page tools are route-scoped — they change whenever the user navigates. Putting
them in `toolConfig` would rewrite a 30k–150k-token prefix at the cache-write
premium on every navigation. That is exactly the failure mode the cost tenet
exists to prevent.

**Therefore:**

- `toolConfig` gains exactly **one** stable entry:
  `page_action(name: str, arguments: object)`. Its schema never varies, so the
  cached prefix is byte-stable for the life of the session whether the user is on
  the chat view or deep in an admin page.
- The *list* of currently available page tools is delivered as **per-turn
  context**, reusing `app_context_dispatch`'s mechanism verbatim: the SPA pushes
  its registry snapshot on navigation, it is stashed on `agent.state`, and
  `merge_and_clear_pending_context` renders it into that turn's prompt only —
  out of persisted history, out of the cached prefix.
- The rendered list is **sorted by name** at its source, per the determinism half
  of the prompt-cache contract.

This is the same lever `docs/specs/tool-search-token-bloat-strategy.md` proposes
for MCP bloat, applied to a source whose churn rate makes it mandatory rather
than merely advisable.

The cost of the meta-tool indirection is one extra hop of naming in the model's
reasoning. That is cheap and bounded. The alternative is unbounded and recurring.

### D4 — Trust boundary

A page tool executes **in the user's session, with the user's permissions**. It
is not a sandboxed iframe with an origin boundary; it is our own first-party code.
That makes the risk profile different from MCP Apps, not smaller.

- **Declarations are first-party only** in v1. Only our SPA's own registry feeds
  the agent. No third-party page contributes tools. (When outbound lands, page
  tools scraped from a remote origin are untrusted input — descriptions are a
  prompt-injection vector — and must be origin-allowlisted and wrapped as
  untrusted data. Out of scope here.)
- **Mutating tools require consent.** Any declaration with
  `requiresConsent: true` (default `true` for anything non-read-only) routes
  through the existing consent prompt before `execute` runs. Reuse
  `mcp-app-consent.service.ts` rather than inventing a second modal.
- **Tools inherit RBAC; they never widen it.** A page tool calls the same
  services the UI calls, under the same session cookie, so the existing
  `get_current_user_from_session` / `require_admin` checks apply unchanged. A
  tool that would need to bypass a UI permission check is a bug, not a feature.
- **Results are echoed into the transcript** as a `tool_use`/`tool_result` pair,
  the way `app_tool_dispatch` already does. The user must be able to see what the
  agent did to their session.

### D5 — RBAC and gating

- Feature flag `SPA_TOOLS_ENABLED`, default **on** with a kill switch, per house
  style. While off, the registry accepts declarations but publishes nothing, the
  `page_action` tool is not registered, and the SSE event never fires.
- `page_action` is a catalog tool (`ToolCategory.BROWSER` already exists in
  `apis/shared/tools/models.py:24`) so it inherits `grantedTools` RBAC and can be
  granted per role.
- It ships `enabledByDefault=False`. A default-on tool that adds a per-turn
  context block for every user in the fleet is a fleet-wide cost event.

## PR sequence

**PR-1 — SPA tool registry + declarations.** Frontend only, no agent path.
`SpaToolRegistry` service, a `declareSpaTool` helper usable from a component's
injection context with automatic teardown, and 2–3 real declarations on one
admin page to prove the ergonomics. Unit tests. Nothing is exposed yet.

**PR-2 — The agent→page call path.** The `page_action` tool, the
`page_tool_call` SSE event, `POST /sessions/{id}/page-tool-result`, the lease-row
side channel, timeout/cap/cancel handling, and transcript echo. This is the PR
that proves the direction and carries the real risk (see R1).

**PR-3 — Per-turn tool listing.** Registry snapshot push on navigation through
the existing `update-context` mechanism, deterministic ordering, and a
`toolConfig` byte-stability test asserting the cached prefix does not move across
a navigation. Verify with the fingerprint hashes on the session's `C#` rows.

**PR-4 — `document.modelContext` projection.** The adapter that exposes the same
registry to third-party agents, probing both `document.` and `navigator.` and
wrapping any pre-existing implementation. Independently valuable, independently
revertible, and the only PR coupled to an unstable external spec.

PR-1 through PR-3 deliver the user-visible capability. PR-4 is the standards bet.

## Risks

**R1 — Latency makes it feel bad.** A page tool round-trips container → SSE →
browser → app-api → lease row → container. If that is seconds, the agent feels
slower than doing the work itself through an app-api endpoint. *Mitigation:*
measure the round trip in PR-2 before building PR-3 on top of it. If the lease-row
poll dominates, tighten the poll cadence for pending page calls specifically
rather than globally. If it is still bad, the design falls back to app-api
endpoints and only the *declaration* layer survives — which is still what PR-4
needs.

**R2 — The model picks the wrong indirection.** `page_action` with a name from a
per-turn list is a weaker affordance than a first-class tool. *Mitigation:*
PR-3 must include an eval on real page-tool selection accuracy, not just a unit
test that the plumbing works.

**R3 — Two ways to do everything.** A page tool and an app-api endpoint can both
"add a user to a role," and they will drift. *Mitigation:* page tools call the
same services the UI calls — they are a *view-scoped* affordance, never a parallel
API. Anything that wants to work without a browser open belongs in app-api.

**R4 — Angular API churn.** `provideExperimental*` is explicitly unstable.
*Mitigated by D1* — we never depend on it; PR-4's adapter does, and it is one file.

**R5 — The user navigates mid-call.** The tool's execution context disappears
between the SSE event and the result. *Mitigation:* the registry stamps a
generation counter; a result from a torn-down registration is dropped and the
call resolves as "page changed," which the model can read and retry.

## Open questions

1. **Does the agent see the page, or only its tools?** Declarations alone give
   the model no idea what is on screen. A `structuredContent` snapshot alongside
   the tool list would help a lot and costs per-turn tokens. Recommend deferring
   to a follow-up with a measured token budget.
2. **Scope of v1 declarations.** Which surfaces get tools first? Admin RBAC pages
   are the highest-value and highest-risk. A read-only surface would prove the
   path with less blast radius.
3. **Does this belong to the Agent Designer primitive set?** A page tool is
   arguably a fifth bindable primitive alongside tools/models/skills/memory. Worth
   deciding before the registry shape ossifies.

## Appendix A — WebMCP surface notes

- Secure-context only (HTTPS, or localhost). Status is W3C CG draft.
- The global moved `navigator.modelContext` → `document.modelContext`; the
  polyfill keeps a deprecated `navigator` alias. Probe both.
- `provideContext()` / `clearContext()` were removed in the March 2026 revision;
  `registerTool()` / `unregisterTool()` are the declaration surface.
- Angular exposes `provideExperimentalWebMcpTools` (bootstrap),
  `declareExperimentalWebMcpTool` (injection context), and
  `provideExperimentalWebMcpForms` (implicit tools from Signal Forms) — the last
  is interesting for us and worth a look once PR-1 lands.
- The native browser implementation routes tool calls to the *browser's own*
  agent. It is not a channel our backend can read. Any host that is not the
  browser must supply its own runtime — which is what D1's adapter does.

## Appendix B — What this defers

Outbound browsing is not cancelled, it is unbundled. When it is decided on its
own merits, the options are wider than they looked:

- **AgentCore Browser** — isolated, live view, session recording, CloudTrail.
  Good for audit and compliance; weak on auth, cost, and bot-blocking. Would add
  Playwright to the inference-api image (measure the size impact) or a hand-rolled
  CDP client.
- **The user's own browser via an extension** — free, already authenticated, no
  CAPTCHA wall. Much larger security surface and an install requirement.
- **Neither.** `fetch_url_content` already covers read-only web content, which is
  most of what users actually ask for.

Note that if outbound ever lands, the WebMCP driver for it is a *third consumer*
of D1's registry shape — the same tool-descriptor plumbing, sourced from a remote
page instead of ours, with an origin allowlist and untrusted-input handling
bolted on. Building inbound first makes outbound cheaper, not more expensive.
