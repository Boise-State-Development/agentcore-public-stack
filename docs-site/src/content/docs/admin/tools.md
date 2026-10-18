---
title: Tools
description: Register and manage agent tools.
sidebar:
  order: 6
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

AI Configuration group. Define tools (including Gateway MCP targets) and their visibility; backed by `admin/tools` and the `/admin/tools` pages.

## External MCP servers: custom headers

An external MCP server (protocol `mcp_external`) can carry up to 10 static
**custom headers**, sent on every request to the server and on
**Discover from server**. Use them for server options that live in a header,
such as GitHub's `X-MCP-Toolsets`.

- Values are stored on the catalog row and shown on the form in plain text, so
  they are configuration, not credentials. A secret belongs in the Secret ARN.
- Names the transport or its authentication already sets are refused:
  `Authorization`, `Proxy-Authorization`, `Cookie`, `Host`, `Mcp-Session-Id`,
  `Mcp-Protocol-Version`, `Accept`, `Content-Type`, `Content-Length`,
  `Transfer-Encoding`, `Connection`, and anything starting `X-Amz-` (SigV4).
- Saving a header change rebuilds the server's client on the next turn.

See [MCP Apps](/agentcore-public-stack/features/mcp-apps/#github-issue-pickers-need-the-context-toolset)
for the GitHub case this exists for.
