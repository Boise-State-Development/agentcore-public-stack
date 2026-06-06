---
title: Tools and Multi-Protocol Architecture
description: Direct, AWS SDK, MCP+SigV4, and A2A tools — and why the default set is intentionally small.
sidebar:
  label: Tools
  order: 2
---

Tools are how the agent does things beyond generating text — fetching a page,
running a calculation, charting data, calling a remote service. The platform is
**multi-protocol**: a single agent loop can call tools that live in very
different places and authenticate in very different ways, all behind one uniform
tool interface.

## Multi-protocol support

The agent can reach a tool through any of four protocols. Each trades locality
for reach: a direct Python function is the simplest and fastest; an A2A agent is
a fully independent service you delegate to.

| Protocol | Where the tool runs | Auth | Status |
| --- | --- | --- | --- |
| **Direct call** | In-process Python function in the agent (`agents/main_agent/tools/`) | None — same process | Available |
| **AWS SDK** | In-process, but calling an AWS service via `boto3` | IAM (the runtime's task role) | Available |
| **MCP + SigV4** | A Lambda behind the AgentCore **Gateway**, exposed as MCP | AWS SigV4 | Available |
| **A2A** | A separate agent in its own AgentCore Runtime | AgentCore auth | Client-only today |

A few notes on the edges:

- **Direct** and **AWS SDK** tools are ordinary `@tool`-decorated functions. The
  difference is only what they touch — local computation versus an AWS API.
- **MCP + SigV4** tools are discovered dynamically from the Gateway, so the tool
  list grows without a code change. See
  [Gateway MCP Targets](/agentcore-public-stack/integrations/gateway-targets/)
  for registering one.
- **A2A** is **client-only** right now: the platform can call out to remote
  agents, but does not yet expose itself as an A2A server.

## An intentionally limited default set

The platform ships with a **deliberately small** built-in tool set. This is a
design choice, not an omission — the goal is a clean, dependency-light starting
point you extend for your own deployment rather than a kitchen sink you have to
prune.

What's included out of the box:

| Tool | Protocol | What it does |
| --- | --- | --- |
| **Calculator** | Direct (Strands built-in) | Evaluates mathematical expressions. |
| **URL Fetcher** | Direct | Fetches and extracts text from web pages, articles, and docs. |
| **Charts & Graphs** | Direct | Builds interactive bar, line, and pie charts from data. |
| **Code Interpreter** | Direct (sandboxed) | Runs Python in a sandbox to generate diagrams and visualizations. |
| **Spreadsheet tools** | Direct (sandboxed) | Lists and analyzes spreadsheet files from the knowledge base or attachments. |

The built-in registry is assembled in `create_default_registry()`
(`agents/main_agent/tools/tool_registry.py`); each tool's display metadata lives
in the `TOOL_CATALOG` (`tool_catalog.py`).

## Flexible by design

The small default set is paired with several extension points, so capability is
something you add rather than something you're stuck with:

- **Add a code tool** — drop a `@tool` function into
  `agents/main_agent/tools/`, register it in `__init__.py`, and it joins the
  registry. (Direct or AWS SDK.)
- **Add a remote tool without deploying agent code** — register a Gateway MCP
  target; its tools are discovered at runtime and merged into the catalog.
- **Delegate to another agent** — point the agent at a remote A2A agent for
  whole workflows it can hand off.
- **Control who sees what** — tool visibility is governed by RBAC and per-agent
  `enabled_tools`, filtered through the `ToolFilter`. See
  [RBAC and Permissions](/agentcore-public-stack/configuration/rbac/).
- **Curate from the admin console** — the
  [Tools admin page](/agentcore-public-stack/admin/tools/) manages the catalog
  and Gateway targets without touching code.

:::note
Because tool selection is filtered per request, the *registered* set and the set
a given user actually sees can differ — that's how the same deployment serves a
locked-down audience and a power-user audience from one catalog.
:::
