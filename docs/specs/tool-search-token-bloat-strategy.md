# Cross-source tool-search strategy for MCP token bloat

Status: analysis / not built
Last updated: 2026-07-04

## Problem

Tool schemas for all four tool sources — built-in (code-interpreter/browser),
external MCP via Lambda (`mcp_external`), Gateway MCP targets, and A2A — are
serialized into the Bedrock `toolConfig` on **every turn**. As the catalog
grows this becomes a large, constant input-token cost (measured as the
`toolTokens` partition by `ContextAttributionHook`). There is no lazy/deferred
tool exposure today.

## The convergence seam

All four sources converge only at
[`BaseAgent._build_filtered_tools()`](../../backend/src/agents/main_agent/base_agent.py) →
flat list → Strands → serialized into Bedrock `toolConfig` every turn.

Any cross-source search MUST live at this seam — it is the only layer that sees
all four sources.

## What each mechanism can cover

- **Gateway semantic search** — the built-in `x_amz_bedrock_agentcore_search`
  tool, enabled at gateway create with `listingMode=DEFAULT`. Embeds each
  registered target's tool metadata into a serverless vector store; the agent
  calls it via MCP `tools/call` and gets the top ~10–15. **Covers Gateway
  targets only** — not built-in, not `mcp_external`, not A2A. We don't consume
  it yet: `FilteredMCPClient.list_tools_sync` lists all gateway tools and ships
  all matching schemas, so gateway tools currently pay full token cost.
  Sub-lever: migrate `mcp_external` → Gateway targets (`protocol=mcp`) to widen
  vector-store coverage.

- **FastMCP tool-search transform** — server-side, searches only that one
  server's own tools. Not privy to our catalog. Legit only as an in-server
  tactic for a single bloated FastMCP server.

- **Anthropic native Tool Search Tool** (`tool_search_tool_bm25_20251119` /
  `_regex_`) — **RULED OUT on our path.** It is a server-side tool; on Bedrock,
  server-side tools are available via `InvokeModel` only, not Converse. Strands
  drives Bedrock via `converse_stream`. (Note: `anthropic_beta` *flags* like
  fine-grained-tool-streaming DO pass through Converse `additionalRequestFields`;
  server-side *tools* do not — different mechanism.)

- **AWS Agent Registry (Preview)** — a managed discovery service exposed as a
  remote **MCP endpoint**. MCP servers, agents, skills, and custom resources are
  published as *records*, validated against protocol schemas, curated through an
  approval workflow, and searched via hybrid semantic+keyword. Auth is IAM or
  corporate JWT. See [Registry as a dynamic tool-discovery tier](#registry-as-a-dynamic-tool-discovery-tier).

## Recommended tiers

0. **Curate first** — assistant/role-scoped `enabled_tools` profiles. Search only
   pays off when ONE assistant needs a large universe but few tools per turn.
1. **Consume Gateway semantic search** for the gateway slice + migrate external
   onto the gateway (mostly AWS-managed, ~80–90% of the win).
2. **Unified `search_tools(query)` facade** at `_build_filtered_tools`,
   delegating the gateway slice to `x_amz_bedrock_agentcore_search` and indexing
   the residual off the DynamoDB catalog / S3 Vectors / Bedrock KB.

### Tier-2 gotchas

Strands fixes the tool list at agent construction (cache keyed on `tools_hash`
in `inference_api/chat/service.py`) → dynamic promotion needs an agent rebuild
or runtime tool registration (the real cost). Must **append** schemas (like
Anthropic native does), not rebuild — a naive rebuild invalidates the prompt
cache after the insertion point on every search and can cost more than the bloat
removed.

## Registry as a dynamic tool-discovery tier

AWS Agent Registry is a *discovery + governance* layer, not an execution layer —
it catalogs and advertises resources but does not run them. That makes it a
candidate **dynamic-discovery** answer to the token-bloat problem, distinct from
the tiers above:

- **How it would cut bloat.** Instead of front-loading every tool schema into
  `toolConfig`, the agent queries the Registry's MCP-native endpoint
  semantically for the right tool/sub-agent at the point of need, and we promote
  only the matched schemas at the seam. This is the same "discover then promote"
  shape as Tier-2, but the index + governance are AWS-managed rather than
  hand-rolled off DynamoDB/S3 Vectors.

- **Coverage vs. Gateway semantic search.** Gateway search covers Gateway
  targets only. Registry can catalog **any** resource type — MCP servers,
  agents, skills, custom records — so in principle it spans sources the Gateway
  vector store never sees (`mcp_external`, A2A leaf agents, Harness sub-agents).
  Registry advertises/governs; the Gateway still *connects* the tool for
  invocation. They are complementary, not overlapping.

- **Bonus: governance for free.** The approval workflow + curation is exactly the
  admin/RBAC posture in the admin-skills plan — an admin curates which tools and
  sub-agents are discoverable, rather than us building that workflow.

- **Still needs the seam.** Registry returns *records* (metadata + how to reach
  the resource), not live Strands tool objects. Discovery via Registry still
  terminates at `_build_filtered_tools` promotion + the Tier-2 append-not-rebuild
  constraint. Registry replaces the *index*, not the promotion machinery.

- **Caveats.** Preview / region-gated — not for a delivery-critical path yet.
  Registries are created via AgentCore control APIs, not CDK — a parallel
  provisioning surface to own (same posture as Harness and Gateway targets).

**Where it sits in the plan:** an alternative index backing Tier-2, and the
natural discovery/governance layer if we adopt Harness-as-leaf-sub-agents. Worth
a spike *after* the Tier-1 Gateway-search measurement, not before — Tier-1 is
lower-risk and mostly AWS-managed.

## Suggested next step

1-day Tier-1 spike — dev gateway `DEFAULT`/semantic, agent calls
`x_amz_bedrock_agentcore_search` instead of the full list, measure `toolTokens`
before/after.

## Related

- `project_gateway_3lo_listing_mode` — `DEFAULT` co-gates 3LO + semantic search
- `project_issue419_gateway_target_registration`
- `project_context_attribution_prototype` — `toolTokens` partition is the measurement
- `project_skills_registry_tool_binding` — Registry for governance/discovery over bound tools
- `docs/specs/admin-skills-rbac-tool-binding.md` — the RBAC/curation posture Registry approval maps onto
