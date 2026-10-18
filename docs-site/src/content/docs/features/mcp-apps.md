---
title: MCP Apps
description: Interactive tool UIs (SEP-1865).
sidebar:
  order: 3
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

ui_resource and ui_tool_input_partial SSE events; sandbox origin wiring.

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
