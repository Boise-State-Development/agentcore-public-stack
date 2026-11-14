# MCP typed tool outputs: measurements and proposal

Status: **Part A agreed and implemented (2026-11-09). Parts B and C are proposals, not agreed.** Written 2026-11-09, after mcp 2.3.0 (protocol 2026-07-28) reached dev in #1666.

Typed outputs mean an `outputSchema` on each tool in `tools/list`, plus `structuredContent` on each result. This doc covers four things. What they cost us. What they break in our own bookkeeping. What they could buy: a bounded payload for the model, and cards in the SPA. How each part is gated.

## 1. Measurements

### Method

The runs called GitHub's remote MCP server (`api.githubcopilot.com/mcp/`) directly, using our pinned client (mcp 2.3.0). There were two sessions, one per protocol:

- one with the legacy `initialize()` handshake, which negotiates `2025-11-25`;
- one with modern `server/discover`, which negotiates `2026-07-28`.

Both sessions made the same read-only calls on this public repo with the same arguments. Tokens were counted with Bedrock `CountTokens` on dev (`anthropic.claude-haiku-4-5-20251001-v1:0`). Each result was counted as the `toolResult` the model would see, with the empty-result baseline subtracted.

This is a cleaner A/B than reading `C#` rows. Rows mix calls, arguments and turns. Here the only variable is the protocol.

### R3: per-result tokens, legacy vs modern

| Call | Legacy tokens | Modern tokens | Δ |
|---|---:|---:|---:|
| `list_issues` (perPage 10) | 5,592 | 5,592 | 0 |
| `list_issues` (defaults, 30 issues) | 16,261 | 16,261 | 0 |
| `issue_read` get | 392 | 360 | −8% |
| `issue_read` get_comments (none) | 1 | 11 | +10 |
| `search_code` (perPage 10) | 7,968 | 7,996 | +0.4% |
| `search_issues` (perPage 10) | 3,795 | 2,460 | **−35%** |
| `get_file_contents` (a file) | 2,726 | 2,726 | 0 |
| `get_file_contents` (a directory, 88 entries) | 28,241 | 16,965 | **−40%** |
| `list_pull_requests` (perPage 10) | 3,652 | 3,652 | 0 |
| `get_me` | 169 | 170 | +1 |

**R3's premise was wrong.** GitHub's legacy success text was not prose. It was already compact JSON. On 2026-07-28 the text becomes the compact JSON of `structuredContent`; in every case, parsing the text gives exactly `structuredContent`. That payload is a trimmed version of the old one:

- it drops `url`, `*_url`, `id` and `node_id` noise;
- it flattens labels to their names;
- it wraps `issue_read` in `{issue: …}`.

Nothing the model needs is gone. Bodies, titles, numbers, states, labels and `html_url` are all still there. **The upgrade is token-neutral or better on every call measured.** There is nothing to undo.

### Where the tokens actually go

What matters is the absolute size, not the format. The tool-result offloader (`core/tool_result_offload.py`) replaces any result over **4,000 tokens** with a **1,000-token preview**, plus a reference the model can read through `retrieve_offloaded_content`. Of the calls above, four always offload:

- `list_issues` (even at perPage 10);
- `list_issues` at its defaults;
- `search_code`;
- a directory listing.

So for "what's open in this repo?", the model sees roughly the first two or three of 30 issues. It then either answers from those or spends a round trip retrieving the rest. That is a quality cost before it is a token cost.

### R2: `toolConfig` stability

Strands copies `outputSchema` onto every MCP `tool_spec` (`mcp_agent_tool.py:85`). Across GitHub's 43 tools that is **+77 KB** of schema: the specs grow from 49.7k to 127.9k characters. I formatted the same 43 specs with and without it:

| Consumer | With vs without `outputSchema` |
|---|---|
| Bedrock Converse `toolConfig` (`BedrockModel.format_request`) | **byte-identical** |
| OpenAI chat `tools` (`OpenAIModel.format_request`) | **byte-identical** |
| OpenAI Responses `tools` (Mantle, `OpenAIResponsesModel._format_request`) | **byte-identical** |
| Our `toolConfigHash` (`prefix_fingerprint.py`, also `handoff_brief.py`) | **changes** |
| Our Responses `prompt_cache_key` (`bedrock_responses.build_prompt_cache_key`) | **changes** |
| Context itemization (`context_itemization.itemize_tools`) | GitHub's apportioned share **~2.6× too large** |

The wire is fine, so no cache re-write happened. Three of our own consumers hash or size the *registered* spec rather than what the model is sent:

1. **`toolConfigHash` rotated once** at the deploy for every GitHub-connected session, while `cacheStatus` stayed `hit`. The cost runbook says "the hash that changed names the cache-buster", so that is a false lead. It will rotate again whenever GitHub edits any output schema, with no wire change. It also hashes 2.6× more characters on every `BeforeModelCall`.
2. **The Responses `prompt_cache_key` rotated** for GPT sessions with GitHub. It is a routing hint, so a new key can route to a cold cache. That costs one write on the deploy, and another on every GitHub schema edit. It is small, but it is a real cost.
3. **The context breakdown overstates GitHub's tools.** The measured tools total is right. The split between tool groups is apportioned by spec size, so GitHub claims tokens that belong to other groups.

### Persistence and the SPA (what Strands already carries)

- `MCPClient` puts `structuredContent` and `metadata` on the `MCPToolResult` dict (`mcp_client.py:1459`).
- No formatter sends either key to a model.
- The dict is the `toolResult` block, so `TurnBasedSessionManager` **persists it in full**.
- `tool_result` SSE carries it. `GET /messages` returns it unchanged (`_process_tool_result_content`). The SPA already types it (`ToolUseData.result.structuredContent`) and reads it for the #1662 awaiting marker.
- **But the offloader drops it.** It rebuilds the result as `{toolUseId, status, content}` (`context_offloader/plugin.py:638`). So the large results, which are exactly the ones a table would help, reach neither the SSE event nor the stored message with `structuredContent`.
- A typed GitHub result is now stored **twice**: once as the text JSON and once as the object. A result over ~50k characters therefore crosses AgentCore Memory's 100k conversational-event limit and is stored as a blob. That is benign (a tool result is not useful long-term-memory material), but it is worth knowing. I read this in code and did not measure it on dev.

## 2. Proposal

There are three independent parts. Each could be its own PR.

### Part A: hash and size what the model is sent (fix, recommended now)

- Add one helper, `model_facing_tool_specs(specs)` in `apis/shared/models/`. It keeps `name`, `description` and `inputSchema`, which is what all three formatters read.
- Have these four consumers use it: `prefix_fingerprint`, `handoff_brief`'s fingerprint, `build_prompt_cache_key`, and `itemize_tools`.
- Add a test that pins the helper against each formatter's output for a spec carrying `outputSchema` and `annotations`. Then a formatter that starts forwarding a field fails CI instead of drifting.

Effect:

- `toolConfigHash` follows the wire again.
- The Responses key stops moving on GitHub schema edits.
- The itemized breakdown is right.
- `BeforeModelCall` hashes less.

**As built.** The helper is `apis/shared/models/tool_specs.py` (`model_facing_tool_specs`). `compute_prefix_fingerprint` projects internally, so `handoff_brief` needs no change of its own. `tests/shared/test_tool_specs.py` formats real Strands requests (Bedrock, Bedrock strict, OpenAI chat, OpenAI Responses) and checks two things: a spec carrying `outputSchema` and `annotations` formats identically to its projection, and every kept key changes the request. Hashing GitHub's 43 typed tools plus 20 plain ones went from **0.68 ms to 0.22 ms** per `BeforeModelCall`.

The cost is one deliberate rotation of `toolConfigHash` (observability only) and of the Responses `prompt_cache_key` (one cache write per live GPT conversation) at the deploy. Specs that already carry `annotations` rotate too, because those were also hashed but not sent.

### Part B: a bounded projection for the model (needs your call)

**Rule:** when a result's text is exactly its `structuredContent` as JSON, the model gets a bounded projection of it instead. The rule only applies when the text is that JSON. That makes it server-agnostic: it can never touch a prose result, and today it fires only for GitHub.

**Projection:**

- Inside list items, truncate any string over 280 characters to 280 characters, followed by `… [N chars; read the item for the rest]`.
- Directory entries keep `type`, `name`, `path` and `size`.
- Singleton results (`issue_read`, `get_file_contents` on a file) are untouched.

Measured on the same payloads:

| Call | Modern | Projected | Offloaded before → after |
|---|---:|---:|---|
| `list_issues` (10) | 5,626 | 1,933 (−65%) | yes → **no** |
| `list_issues` (30) | 16,387 | 5,577 (−65%) | yes → yes |
| `search_issues` (10) | 2,491 | 791 (−68%) | no → no |
| `search_code` (10) | 8,376 | 4,212 (−49%) | yes → yes (just over) |
| directory (88) | 16,965 | 3,305 (−80%) | yes → **no** |
| `list_pull_requests` (10) | 3,665 | 1,321 (−63%) | no → no |

The quality argument for it:

- A 10-issue list or a directory now reaches the model **whole**, instead of as a 1,000-token preview of its first few entries.
- A 30-issue list still offloads, but its preview covers about three times as many issues.
- Results under the gate save about 1–2k tokens each, for the life of the session.

The quality risk is a question that hinges on text past character 280 of a body in a *list* result. The truncation note tells the model to read the item, and `issue_read` is one call away.

**Where it lives:**

- A pure function in `apis/shared/mcp_results/projection.py`.
- An `AfterToolCallEvent` hook in `agents/main_agent/session/hooks/tool_result_projection.py`. It **replaces** `event.result`, the `WebSearchDefaultsHook` pattern, and must run **before** the offloader; Strands runs After-callbacks in reverse registration order, so the test must pin the order.
- It keeps `structuredContent` and `metadata` on the result, so the SPA and the #1662 marker see the full object.

Why not `document_cap`'s request-only projection? The offloader decides at `AfterToolCall`, before any request is formatted. A request-time projection would only ever see the offloader's preview.

Contract checks:

- Applied once, at intake, then stored, so history stays byte-stable and append-only. Nothing in the cached prefix moves.
- Not on the TTFT path. It runs between tool rounds, at a cost of one JSON parse and compare of the text. I'd measure that on the 56 KB payload and state it in the PR.
- I'd want an eval-harness run on GitHub list questions before default-on.

### Part C: SPA cards for typed results (later, if you want it)

The display data has to survive the offloader, and it shouldn't depend on the copy inside the content block. So:

1. The Part B hook, or a sibling hook if B is declined, writes a bounded display projection of `structuredContent` to a `TRES#<toolUseId>` row in sessions-metadata, using the existing `SessionLookupIndex` GSI (the `TSUM#` / `SRCV#` / `EXTR#` pattern, no new infrastructure).
2. It emits a new `tool_structured_result` SSE event, `{type, sessionId, toolUseId, server, tool, schemaVersion?, data}`. The event is drained in `stream_coordinator` with `tool_group_summary` and replayed on `GET /messages` as `structuredResults`.
3. The SPA gets renderers in `tool-renderer-registry`: an issue/PR table, a code-search list and a file tree, keyed on server and tool name, with a raw-JSON fallback. They render under the tool rail.
4. The TUI ignores an unknown event (it logs it once), so the event is additive for both clients.

Separately, we could stop persisting the second copy of `structuredContent` once the marker reads it from `TRES#`. That is out of scope here.

## 3. Gating (per CLAUDE.md's Feature Flags section)

| Part | Kind | Why |
|---|---|---|
| A | **No flag** | It fixes our bookkeeping, and the wire doesn't change. A flag would guard a hash. |
| B | **In-development flag** (`TOOL_RESULT_PROJECTION_ENABLED`, Runtime only, written only while on), then a default-on **rollout switch** | The change is meant to be invisible in answer quality, so once it's proven it retires on the default condition: shipped in a prod release, two weeks there with the switch never flipped, and a readout recorded. Line in #1422. |
| C | **In-development flag** on both sides (`MCP_RESULT_CARDS_ENABLED` + `features.mcpResultCards`), then a **feature switch** | It is a new UI surface built over several PRs, and it writes a row per typed call, which is a cost a deployment may not want. |

## 4. Decisions needed

1. **Part A**: go? (Recommended. It's small and has no flag.)
2. **Part B**: build it behind an in-development flag, with an eval-harness readout before promotion? Is 280 characters the right cap?
3. **Part C**: now, later, or not at all?

## Appendix: reproduce

The A/B script and the CountTokens counter used for this doc are not checked in. To reproduce, connect `mcp.client.session.ClientSession` over `streamable_http_client` twice, calling `initialize()` on one session and `discover()` on the other, with `X-MCP-Toolsets: issues,repos,pull_requests,context`. Then count each result's text with CountTokens, as in the memory recipe for tool-spec measurement. If B goes ahead, a checked-in `scripts/measure_tool_result_projection.py` would make it repeatable.
