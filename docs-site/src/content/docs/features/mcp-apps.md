---
title: MCP Apps
description: Tools that render their own interactive UI inline in the conversation, framed in an isolated sandbox origin (SEP-1865).
sidebar:
  order: 3
---

An MCP server can ship a small web UI alongside a tool. When the model calls
that tool, the conversation shows the App in a frame instead of a plain tool
card: a drawing canvas, a task board, an issue form with pickers. The App can
call tools on its own server, open links, go fullscreen and tell the model what
the user did. This is the MCP Apps extension (SEP-1865), and the platform is a
host for it.

It works with any MCP server the agent reaches, external servers and Gateway
targets alike. Nothing is configured per App: a server that declares UI on its
tools gets it rendered.

On by default. `AGENTCORE_MCP_APPS_HOST_ENABLED=false` on the AgentCore Runtime
opts an environment out; there is no `CDK_*` variable or SPA flag for it. With
it off, the platform advertises nothing to MCP servers and hides nothing from
the model, so every tool behaves as a plain tool.

## How a tool gets a UI

The agent advertises the `io.modelcontextprotocol/ui` extension on every MCP
`initialize`, so a server knows the host can render Apps. A server that
understands it marks tools with `_meta.ui` in its `tools/list` response, naming
a `ui://` resource whose content is `text/html;profile=mcp-app`.

`_meta.ui.visibility` says who may call a tool:

| Visibility includes | Effect |
|---|---|
| `model` | The model sees and can call the tool, as usual |
| `app` | The App may call the tool from its frame |
| `app` only | The tool is hidden from the model. Only the App calls it |

App-only tools never reach the model's tool list, so they add nothing to the
cached prompt. They also pass an external server's **Available Tools** list
automatically (see the section on GitHub below).

## What happens during a turn

When the model starts calling a UI tool, the App frame mounts before the
model has finished writing the arguments:

1. At the tool's `content_block_start`, the runtime emits a header-only
   `ui_resource` event (empty `html`). The SPA swaps the plain tool rail for the
   App frame's header (icon, server name, tool name and a running shimmer)
   with no flash.
2. The runtime fetches the App's HTML from the server with `resources/read`
   and emits the full `ui_resource` event. The SPA mounts the iframe once
   `html` is non-empty. Each `toolUseId` is emitted at most once, and the
   header shell is tracked separately so it never blocks the full event.
3. While the model is still streaming the arguments, `ui_tool_input_partial`
   events carry the arguments so far. JSON prefixes are rarely valid, so the
   runtime "heals" each one into the largest valid object it can (closing open
   strings, brackets and braces). The SPA forwards each to the App as
   `ui/notifications/tool-input-partial`, which lets an App such as
   Excalidraw animate while arguments arrive.
4. When the arguments are complete, the App gets `tool-input`, then the
   `tool-result`.

If the tool's name was not known at block start, the `ui_resource` event
falls back to right after the matching `tool_result`.

### The frame header

The header names the server and shows its icon, like Claude's own App frames.
The server name comes from the server's MCP `initialize` `serverInfo` (`title`,
then `name`), falling back to the title-cased `ui://` authority. The icon comes
from `serverInfo.icons`, then from the server's MCPB `manifest.json` (fetched
from `<server-origin>/manifest.json`, same origin only, cached per origin), and
otherwise a generic glyph.

The event payload is
`{type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin, serverName, icon, toolName}`.
The [SSE Streaming](/agentcore-public-stack/features/sse-streaming/) page lists
every event type.

## What an App can do

An App talks to the host over `postMessage` (JSON-RPC 2.0, spec version
`2026-01-26`). The host supports:

| Request | What the host does |
|---|---|
| `tools/call` | Runs a tool on the App's own server and returns the result (see below). Refused unless the tool's visibility includes `app` |
| `ui/message` | Treated as if the user typed it: starts a normal turn |
| `ui/update-model-context` | Holds the content for the model's next turn |
| `ui/open-link` | Asks the user first, then opens the link |
| `ui/request-display-mode` | Switches between inline and fullscreen (picture-in-picture falls back to inline) |

### Calling tools from the App

A button press in the App becomes a `tools/call`. The SPA relays it to
`POST /mcp-apps/proxy-call` on the App API, which authenticates the session
cookie and forwards it to the AgentCore Runtime. The runtime runs the single
tool with no model turn, on the conversation's live agent when one holds the
right MCP client (matched on session and user), and builds one only when
there is none.

Each call is recorded as a card under the frame, so the frame header shows a
chip such as `3 actions · 1 failed`. Expanding it lists the successes in one
summary and each failure with its error. Cards are keyed by the call itself,
so an App that re-runs its setup calls on every mount updates its cards
instead of adding new ones. The cards are a record for the user only: none of
it reaches the model.

If the MCP connection drops mid-call, the App is told the action may have gone
through, so the user checks before retrying, and the client reconnects on the
next press.

### Telling the model

`ui/update-model-context` goes to `POST /mcp-apps/update-context`. The content
is held in a small in-process store keyed by `(session, user)`, and the next
turn prepends it to the user's new message, after the cached prompt prefix.
Runtime affinity pins that pair to one container, so the push and the next turn
meet there. This is how the model knows, for example, that the user submitted
the issue the App's form created.

### Permissions

Opening a link, and any sandbox capability an App declares in
`_meta.ui.permissions` (`camera`, `microphone`, `geolocation`,
`clipboardWrite`), needs the user's yes. The prompt appears inside the App's
frame, so it stays answerable in fullscreen. Prompts are not saved: a pending
one is denied if the user leaves the conversation, and nothing is remembered
across a reload.

## Forms that wait for the user

Some App tools hand the write to a form instead of doing it. GitHub's
`issue_write` and pull-request writes answer the model's call with
`isError: true` on purpose, so the model does not claim the write happened,
while the App shows a form with a **Submit** button.

The runtime recognises that answer and marks the result
`structuredContent.status = "awaiting_user_submission"`. The frame header, the
response strip and the tool rail then show a neutral **Waiting for you** rather
than **Failed**, `agent_status` reports the call as ok, and a batch of only
waiting calls is headed "Waiting for you to submit the form in" the
integration's name. Only `structuredContent` changes, which no model
formatter sends, so the model still reads the call as not done and the prompt
is unchanged.

## After a reload

The `ui_resource` event never re-streams. To bring an App back, the runtime
stores each model-initiated UI resource as gzipped HTML in the sessions
metadata table (`UIRES#<toolUseId>`, 90-day TTL), and `GET /messages` replays
it so the SPA re-mounts the frame. The action cards are stored the same way
(`APPCARD#`). This needs no extra infrastructure.

Two size limits apply, both from DynamoDB's 400 KB item limit:

- An App whose HTML is still over about 380 KB after gzip is not stored. It
  works live and reloads as a plain tool card.
- A large auto-fetched icon is not stored. The header shows it live and
  reloads with the generic glyph.

## The sandbox origin

An App's HTML is untrusted, so it never runs on the SPA's origin. The SPA
frames it on a separate CloudFront origin, `mcp-sandbox.{domain}`, which every
deployment gets as part of `PlatformStack`. That distribution serves
`proxy.html` from its own S3 bucket. The proxy loads the App into an inner
iframe (`allow-scripts allow-same-origin allow-forms`) and relays messages,
each carrying a per-frame nonce.

The CSP is set per App. A CloudFront Function builds the
`Content-Security-Policy` header from a `?csp=` query parameter, which the SPA
adds only when the App declares domains in `_meta.ui.csp` (`connectDomains`,
`resourceDomains`, `frameDomains`, `baseUriDomains`). `frame-ancestors` is
locked to the SPA origin, so nothing else can frame the proxy. The SPA's own
CSP allows framing the sandbox origin in `frame-src`.

The runtime learns the origin from `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`, which
CDK sets from the distribution, and sends it on every `ui_resource` event as
`sandboxOrigin`. An empty origin means the SPA has nowhere to frame the App.

| Variable | Purpose |
|---|---|
| `CDK_MCP_SANDBOX_CERTIFICATE_ARN` | A us-east-1 ACM certificate covering `mcp-sandbox.{domain}`. Falls back to the shared CloudFront certificate. Synth fails when a domain is configured and neither is set, because the proxy would deploy without a DNS record and no App could load |
| `CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS` | Comma-separated extra origins allowed to frame the proxy, for example `http://localhost:4200` for a local SPA pointed at a deployed environment. Leave it empty in production |

A stack without a domain serves the proxy on the CloudFront default domain.

Testing on `localhost:4200` bypasses CloudFront's response headers, so a CSP
problem on a deployed environment may not show locally.

## GitHub issue pickers need the `context` toolset

GitHub's MCP App for `issue_write` fills its label, assignee and milestone
pickers by calling `ui_get`, an app-only tool (hidden from the model, callable
by the App). `ui_get` lives in GitHub's **`context`** toolset, and the
per-toolset URLs (`https://api.githubcopilot.com/mcp/x/issues`) serve only the
toolset they name. On those URLs the App's call fails with
`unknown tool "ui_get"`, and `/mcp/x/issues,context` lists no tools at all.

Configure the connector on the base URL and choose the toolsets with a header:

| Field | Value |
|-------|-------|
| Server URL | `https://api.githubcopilot.com/mcp/` |
| Custom header | `X-MCP-Toolsets: issues,context` |

Keep the **Available Tools** list to the tools the model should see.
App-only tools such as `ui_get` pass that list automatically, so the model's
view does not change. Without the header the base URL serves GitHub's whole
default toolset, so the header keeps what the server exposes to what you use.
See [Tools](/agentcore-public-stack/admin/tools/#external-mcp-servers-custom-headers).
