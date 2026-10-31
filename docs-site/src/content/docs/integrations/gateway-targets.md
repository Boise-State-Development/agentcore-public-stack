---
title: Gateway Targets
description: MCP over SigV4 via AgentCore Gateway, including the built-in Web Search connector.
sidebar:
  order: 2
---

:::caution[Draft]
Only the built-in connector section below is written. The rest of this page is a placeholder.
:::

DEFAULT vs DYNAMIC listing mode and 3LO consent.

## Built-in connectors: AgentCore Web Search

AgentCore Web Search is a search tool that AWS runs behind the Gateway. You don't deploy an endpoint or manage an API key, and queries are not sent to a third-party search engine. Results come from an Amazon-operated web index: relevant snippets, each with its source URL, title and publication date.

It is registered like any other Gateway target, through the admin tool form, so the catalog row, role grants, the user's tool picker and the agent's tool filtering all work as they do for an external MCP server.

### Register it

1. **Admin → Tools → New tool**, protocol **MCP Gateway (AgentCore)**.
2. Under **Target type**, choose **AgentCore Web Search**. The form fills in any empty fields: tool id `agentcore_web_search`, name, description, category Search, and target name `web-search`.
3. Optionally set domain filters (below), then **Save**. Saving creates the Gateway target and then the catalog row. If the target fails to create, the row is not saved.
4. Grant the tool to a role (or a user) like any other tool.

The agent sees one tool, `web-search___WebSearch`, which takes a `query` (up to 200 characters), an optional `maxResults` (1–25, default 10), and optional per-request domain and published-date filters.

### What the platform fixes for you

| Setting | Value | Why |
|---|---|---|
| Outbound credential | Gateway execution role | The connector is AWS-operated. CDK grants the role `bedrock-agentcore:InvokeWebSearch` on `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1`. |
| Connector version | Pinned (default `1.3.0`) | The Gateway snapshots the tool schema at registration, and that schema is part of every turn's cached `toolConfig`. Pinning means a new default version from AWS can't change it unannounced. To adopt a new version, set it on the tool and save. |
| Listing mode | Default | Gateway semantic search requires it. |
| Tool list | `WebSearch` | This list is what expands the catalog id to the Gateway tool name. Per-tool flags (always on, needs approval) are kept. |

A target can't be switched between an external server and a connector. Delete the tool and recreate it.

### Domain filters

- **Only search these domains**: every search is limited to these domains.
- **Never return these domains**: results from these domains are always dropped.

Enter bare domains, one per line (`boisestate.edu`, not `https://boisestate.edu/`). A domain also covers its subdomains. Each list holds up to 100 domains. The agent never sees these lists. Its own per-request filters can narrow results further but cannot widen them.

### Cost and terms

- AWS bills **$7 per 1,000 searches**. Nothing is billed until the target exists and a role is granted the tool. The IAM grant alone costs nothing.
- Results add to each turn's prompt only for the turn that searched. Ten results is the default, and the snippets are already trimmed for model context.
- AWS's acceptable-use terms require any answer built on search results to keep and show their source citations and links. Grant the tool to Agents and roles whose answers cite their sources.
- Available in us-east-1, us-east-2, us-west-2, eu-west-1, ap-southeast-2, ap-northeast-1 and us-gov-west-1. The Gateway's region must be one of these.

### Comparing with an external search MCP server

Both can be registered at once under different tool ids. To compare them on the same questions, grant both to a test role. Then use `GET /admin/costs/sessions/{id}/calls` to compare what each tool's results added to the prompt.
