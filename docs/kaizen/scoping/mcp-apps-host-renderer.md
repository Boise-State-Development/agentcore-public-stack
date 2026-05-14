# Scoping — MCP Apps Host Renderer

> Status: Scoping (no code yet)
> Owner: Phil Merrell
> Source: research/2026-05-10.md ▸ Top 6 #1 ▸ Agentic UI/UX | reviews/2026-05-10.md ▸ Proposal #1 (Ship — scope this week) | review-queue.md (open)
> Spec read: `specification/2026-01-26/apps.mdx` (normative). Pre-merge step: diff `specification/draft/apps.mdx` against the dated version to catch any movement before PR #1 lands.

## Goal

Implement the host side of the MCP Apps extension (SEP-1865) end-to-end and to spec, so that any MCP server we connect — Gateway-hosted or external — can return interactive UIs alongside text/JSON tool results, and so our chat sits on the agentic-UI standard that Claude Desktop, ChatGPT, VS Code Copilot, Goose, Postman, and MCPJam already meet.

**Out of scope:** authoring MCP Apps (we are a host, not a server-of-apps), MCP-UI / `@mcp-ui/client` framework adoption (we implement the postMessage protocol directly), and any non-MCP-Apps "generative UI" pattern.

## Architectural decisions (locked)

These four were the open ones from scoping. Decisions, with rationale.

### 1. Sandbox origin — new subdomain (Sandbox Proxy pattern)

Stand up a dedicated origin for the outer "sandbox proxy" iframe so `allow-same-origin` does not give iframe content access to the main `ai.client` origin. Pattern matches Claude.ai's web-host implementation.

- **Origin:** `mcp-sandbox.<our-domain>` (exact name TBD in PR #1 — see CDK work).
- **What it serves:** a single static `proxy.html` shell that itself creates the inner content iframe via `srcdoc` (the inner iframe is where the MCP App HTML actually runs). The outer page is what `ai.client` `postMessage`s to.
- **Why two iframes:** the spec's "Sandbox Proxy pattern" for web hosts — the inner iframe takes the strict CSP from `_meta.ui.csp`, the outer iframe gives us a stable cross-origin boundary against the host page.
- **CDK:** new stack `infrastructure/lib/mcp-sandbox-stack.ts` — CloudFront distribution, S3 bucket for `proxy.html`, ACM cert. Flowed through the `cors-deployment` skill for origin allowlisting.

### 2. App-initiated `tools/call` — pipe through inference-api dispatch

When the iframe calls `tools/call`, we surface it as a `tool_use` / `tool_result` event in the active conversation stream. Provenance is preserved — the chat history is a complete audit trail of what the embedded app ran on the user's behalf.

- **Path:** iframe `postMessage` → frontend `mcp-app-frame` → app-api (new endpoint `POST /mcp-apps/proxy-call`) → inference-api → MCP server → reverse path. The inference-api side synthesizes a `tool_use` event into the conversation's SSE stream so it lands in the user's chat thread.
- **Conversation correlation:** the iframe is bound to the originating `toolUseId` and conversation session at render time; proxied calls inherit that binding.
- **Visibility enforcement:** the proxy endpoint MUST reject calls for tools whose `visibility` does not include `"app"` — at both the app-api boundary and the inference-api dispatch.

### 3. `ui/update-model-context` storage — Strands `agent.state`

App-supplied context (the structured/text payload from `ui/update-model-context`) lives in Strands `agent.state` under a dedicated key (e.g., `mcp_apps.context[resourceUri]`). This is where the upstream reference repo moved its compaction state on Apr 27 (commit `2b1a13d`) and it's where Strands is heading.

- **Read path:** before each inference turn, merge any pending `agent.state.mcp_apps.context.*` entries into the prompt context, then clear them.
- **Spec semantics honored:** "host MAY defer context until next user message" and "host SHOULD only send last update if multiple arrive before next user message" — we dedupe by `resourceUri` and apply last-write-wins between turns.

### 4. v1 method scope — full set, no deferrals

Implement every `ui/` method the spec defines and every standard MCP method it permits inside the postMessage channel. Rationale: the user-facing payoff of MCP Apps is highest when the app can both *receive* context (host→app) and *push* it back (app→host) — half-implementing either side cuts off the workflows the spec exists to enable (`ui/message`, `ui/update-model-context`). One feature flag (`MCP_APPS_HOST_ENABLED`) gates the whole surface during rollout.

## Spec compliance checklist

Normative requirements from `apps.mdx` (2026-01-26). Items prefixed with `MUST` are spec-mandated; `SHOULD`/`MAY` items captured in the PR-level acceptance criteria below.

- **MUST** fetch UI resources via `resources/read` against the `ui://` URI from `_meta.ui.resourceUri` — never inline.
- **MUST** treat `text/html;profile=mcp-app` as the resource MIME type.
- **MUST** advertise `capabilities.extensions["io.modelcontextprotocol/ui"]` with `{ mimeTypes: ["text/html;profile=mcp-app"] }` on every outbound MCP `initialize` (Gateway client + external MCP client).
- **MUST** filter tools whose `_meta.ui.visibility` excludes `"model"` from the agent's tool list (Strands tool registry filter).
- **MUST** reject `tools/call` proxied from the iframe for tools whose visibility excludes `"app"`.
- **MUST** set iframe `sandbox="allow-scripts allow-same-origin"` minimum; add `allow-camera` / `allow-microphone` / `allow-geolocation` / `allow-clipboard-write` only if the resource declares them in `_meta.ui.permissions`.
- **MUST** build CSP from `_meta.ui.csp.{connectDomains, resourceDomains, frameDomains, baseUriDomains}` and apply the spec's deny-by-default defaults. **MUST NOT** allow undeclared domains.
- **MUST** wait for `ui/notifications/initialized` from the app before sending any request or notification.
- **MUST** send `ui/notifications/tool-input` with the complete arguments exactly once (before `tool-result`).
- **MUST** send `ui/resource-teardown` before tearing the iframe down.
- **MUST** accept `event.origin === "null"` from the sandbox iframe and rely on a per-frame nonce instead of origin matching.
- **MUST** correlate JSON-RPC over postMessage using request `id` (standard JSON-RPC 2.0 envelope: `{jsonrpc, id, method, params}` / `{jsonrpc, id, result|error}`).

## PR sequence

Targets `develop`. Each PR is independently mergeable behind `MCP_APPS_HOST_ENABLED=false` until PR #6 flips it on.

### PR #0 — Tool-renderer registry (pre-work; proposal #3 from reviews/2026-05-10.md)

- **Files:** [tool-use.component.ts](frontend/ai.client/src/app/session/components/message-list/components/tool-use/tool-use.component.ts) + new `tool-renderer-registry.service.ts`.
- **Change:** lift the implicit tool-result switch in `ToolUseComponent` into a signal-backed registry keyed by tool name. Default renderer is today's behavior (text/JSON/image). Registry exposes `register(toolName, component)`.
- **Why first:** the MCP App renderer in PR #4 plugs in as just-another-registered-renderer — no special-case branches in `tool-use.component.html`.
- **Acceptance:** all existing tool renderings work unchanged; no MCP App code yet.

### PR #1 — Sandbox-proxy origin (CDK)

- **Files:** new `infrastructure/lib/mcp-sandbox-stack.ts`, updates to [`bin/agentcore-public-stack.ts`](infrastructure/bin/agentcore-public-stack.ts) and `cors-deployment` workflow env vars.
- **Change:** CloudFront + S3 + ACM for `mcp-sandbox.<domain>`; deploy a static `proxy.html` shell implementing the outer-iframe half of the Sandbox Proxy pattern. CSP `frame-ancestors` permits the `ai.client` origin only.
- **Acceptance:** `mcp-sandbox.<domain>/proxy.html` serves; `ai.client` can `postMessage` to it; no MCP server wiring yet.
- **Coordinates with** the [cors-deployment skill](.) — every new env var that names this origin flows through that skill.

### PR #2 — Backend: MCP `initialize` extension advertisement + tool-visibility filter

- **Files:** [external_mcp_client.py](backend/src/agents/main_agent/integrations/external_mcp_client.py), [gateway_mcp_client.py](backend/src/agents/main_agent/integrations/gateway_mcp_client.py), [models.py](backend/src/apis/shared/tools/models.py) (add `visibility` to `ToolDefinition`), Strands tool registry adapter.
- **Change:** advertise `io.modelcontextprotocol/ui` on outbound MCP `initialize`; parse `_meta.ui` off `tools/list` responses onto `ToolDefinition`; filter model-invisible tools out of the Strands agent's tool list.
- **Acceptance:** unit tests covering a fake MCP server returning a UI-bearing tool — confirm visibility filtering, confirm `_meta.ui.resourceUri` survives the round-trip into our tool catalog.

### PR #3 — Backend: SSE `ui_resource` event + `resources/read` fetch path

- **Files:** [event_formatter.py](backend/src/agents/main_agent/streaming/event_formatter.py), [tool_result_processor.py](backend/src/agents/main_agent/streaming/tool_result_processor.py), [stream_processor.py](backend/src/agents/main_agent/streaming/stream_processor.py), and a new helper for `resources/read` against the MCP server hosting the tool.
- **Change:** when a tool result references `_meta.ui.resourceUri`, fetch the resource via `resources/read` and emit a new `ui_resource` SSE event: `{type, toolUseId, resourceUri, html, mimeType, csp, permissions}`. Update [CLAUDE.md](CLAUDE.md) SSE event table.
- **Acceptance:** integration test — fake MCP server returns `_meta.ui.resourceUri`; backend emits `ui_resource` event with HTML body inline. **Spec note:** we still call `resources/read` (spec MUST); we just inline the HTML in the SSE event so the frontend doesn't need its own MCP client.

### PR #4 — Frontend: `<mcp-app-frame>` component + postMessage bridge

- **Files:** new `mcp-app-frame.component.ts`, [stream-parser-types.ts](frontend/ai.client/src/app/shared/utils/stream-parser/stream-parser-types.ts), [stream-parser-core.ts](frontend/ai.client/src/app/shared/utils/stream-parser/stream-parser-core.ts), [stream-parser.service.ts](frontend/ai.client/src/app/session/services/chat/stream-parser.service.ts), wire-in via PR #0's renderer registry.
- **Change:** Angular component that:
  - Renders the outer iframe pointed at `mcp-sandbox.<domain>/proxy.html` with the spec-mandated `sandbox` attribute.
  - Posts a `sandbox-resource-ready` notification to the proxy with `{html, sandbox, csp, permissions}` from the SSE `ui_resource` event.
  - Implements the host half of the JSON-RPC 2.0 envelope over postMessage with a per-frame nonce.
  - Handles `ui/initialize`, `ui/notifications/initialized`, `ui/notifications/size-changed`, `ui/open-link`, `ui/request-display-mode` (inline/fullscreen/pip), `ui/notifications/host-context-changed`, `ui/resource-teardown`.
  - Wires `ui/notifications/tool-input` + `tool-input-partial` + `tool-result` + `tool-cancelled` from the active SSE stream.
- **Acceptance:** load the [basic-host](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples/basic-host) reference's QR-server-style example against our component end-to-end in dev.

### PR #5 — Backend + frontend: app-initiated `tools/call` proxying (decision #2)

- **Files:** new `POST /mcp-apps/proxy-call` route in [app_api](backend/src/apis/app_api), tool-dispatch hook in inference-api to inject a synthesized `tool_use` into the active SSE stream, frontend wiring in `mcp-app-frame.component.ts`.
- **Change:** iframe `tools/call` → app-api → inference-api → MCP server → result → synthesized `tool_use`/`tool_result` events on the conversation SSE stream → frontend pushes `ui/notifications/tool-result` back to the iframe.
- **Acceptance:** clicking a button inside a hosted MCP App that triggers a server tool — the call shows up as a tool-use card in the chat *and* the iframe gets the result via `ui/notifications/tool-result`.
- **Open implementation question:** how to inject a synthesized event into a *closed* SSE stream (i.e., when the iframe lives past the originating turn). Likely a per-conversation event broker the active SSE handler subscribes to; if no handler is active, the call still runs but the chat thread shows it when the user next opens a stream. Detailed design lives in the PR.

### PR #6 — Backend: `ui/message`, `ui/update-model-context`, `ui/open-link` consent, capability gating

- **Files:** [oauth_consent.py](backend/src/apis/shared/oauth/oauth_consent.py) (pattern model), new `ui_capability_consent.py` hook, Strands `agent.state` integration for `mcp_apps.context.*`, conversation-message injection for `ui/message`.
- **Change:**
  - `ui/update-model-context` writes to `agent.state.mcp_apps.context[resourceUri]`, merged into the next turn's context.
  - `ui/message` injects a user-role message into the conversation (treated identically to a typed message).
  - `ui/open-link` is gated by an `openLinks` capability declared in `hostCapabilities`; per-link consent reuses the [oauth-consent-prompt.component.ts](frontend/ai.client/src/app/session/components/message-list/components/oauth-consent-prompt/oauth-consent-prompt.component.ts) pattern (new `ui_consent_required` SSE event family).
  - Camera / microphone / geolocation / clipboard-write capability gating wired through `hostCapabilities.sandbox.permissions`.
- **Acceptance:** an MCP App can mutate model context, post a user message, request to open a link, and request mic access — each triggers the correct host behavior (deferred merge, conversation message, consent prompt).

### PR #7 — Dogfood + enable flag flip

- **Files:** documentation, example MCP App registration, [CLAUDE.md](CLAUDE.md) update, feature flag default.
- **Change:** register one of the [ext-apps/examples](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples) servers (recommended: `scenario-modeler-server` or `budget-allocator-server` — form-style, exercises `update-model-context` and `tools/call` proxying without 3D/charting infra). Flip `MCP_APPS_HOST_ENABLED=true`. Add a runbook entry to docs explaining how to register a new MCP App server.
- **Acceptance:** end-to-end conversation in dev that invokes the example tool, renders the iframe, drives the form, calls back into MCP, mutates model context, and the model picks up the context on the next turn.

## Defaults applied without explicit user call

These were small enough that I'm noting them here rather than putting them in the question set:

- `ToolDefinition` gets a new `visibility: Literal["model", "app"]` list field (default `["model", "app"]` per spec).
- Outbound MCP clients advertise `io.modelcontextprotocol/ui` unconditionally — no per-server opt-in. Servers that don't understand the capability ignore it.
- Iframes persist for the lifetime of the conversation; teardown happens on conversation reset, on explicit user dismiss, or on tab close. No per-turn teardown.
- Default display mode is `inline`; fullscreen and PiP supported in PR #4.
- Per-frame nonce, generated client-side, used to authenticate every postMessage exchange (origin will be `"null"` in srcdoc inner iframes; nonce is the real check).
- Theming: `hostCapabilities.theme` exposes `light` | `dark` at initialize; `ui/notifications/host-context-changed` pushes updates when the user toggles theme.

## Risks and unknowns

- **CSP / `frame-ancestors` interplay.** The outer `mcp-sandbox` origin needs `frame-ancestors` permitting `ai.client`; the inner iframe needs CSP composed from `_meta.ui.csp`. We don't have prior art for nested CSP in our stack — expect 0.5–1 day of CSP debugging on PR #1.
- **`tools/call` proxy when the SSE stream is idle.** PR #5's "inject synthesized event into a closed SSE stream" needs a small event broker. If we punt it, app-initiated tool calls work but the chat thread misses them until the user opens a new turn. Acceptable for a v1; flag as known limitation if we ship without the broker.
- **Spec drift.** `specification/draft/apps.mdx` may have moved since 2026-01-26. Diff before PR #1 lands; if there's material movement, adjust PRs #2–#4 accordingly.
- **AgentCore Gateway pass-through of `_meta`.** Confirm `_meta.ui.resourceUri` survives Gateway's MCP proxying — if Gateway strips unknown `_meta` keys, PR #2 needs Gateway-side work too. Verify in PR #2's integration test.
- **Strands `agent.state` schema.** Our `TurnBasedSessionManager` doesn't currently round-trip `agent.state` through long-term memory. PR #6 may need a small adjacent change to ensure `mcp_apps.context.*` survives turn boundaries.

## Definition of done

- All seven PRs land on `develop` behind `MCP_APPS_HOST_ENABLED=false`; PR #7 flips it on.
- One example MCP App from `ext-apps/examples` runs end-to-end in dev.
- Every MUST in the compliance checklist has a corresponding test (unit or integration).
- The dogfood scenario in PR #7 exercises: resource fetch, iframe render, `tool-input` push, app-initiated `tools/call`, `ui/update-model-context` mutating the next turn, `ui/open-link` consent prompt.
- CLAUDE.md SSE event table updated with `ui_resource` and `ui_consent_required` rows.
- A runbook entry describes how to register a new MCP-Apps-capable MCP server (one section in the docs, no separate doc).

## Timeline

3–4 weeks across calendar, depending on review cadence:

| PR | Effort | Notes |
|---|---|---|
| #0 renderer registry | 0.5d | low-risk refactor |
| #1 sandbox CDK | 1–1.5d | CDK + CORS skill + DNS + cert |
| #2 backend MCP capabilities | 1d | + Gateway pass-through verification |
| #3 backend SSE event | 1d | |
| #4 frontend iframe + bridge | 2–3d | postMessage protocol surface is wide |
| #5 tools/call proxying | 2d | + event broker for idle streams (or punt as known limit) |
| #6 message/context/consent | 2d | reuses oauth-consent pattern |
| #7 dogfood + flag flip | 0.5–1d | |

Total: ~10–12 engineering days, sequenced; parallelization possible after PR #2 lands (frontend can race backend on #3–#4).
