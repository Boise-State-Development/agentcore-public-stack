# Multi-agent patterns — where they meet inter-agent communication

**Status:** Brainstorm. Not scheduled, and nothing here is a commitment. It records how the common multi-agent
patterns map onto this platform, so the inter-agent communication work (handoffs, consults, messages between
conversations) is built in a way that later pattern work can reuse instead of rebuild.
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-10-13
**Targets branch:** `develop`
**Kept current by:** the weekly `kaizen-research` standing watch (source 2b), which checks upstream and our own code
against this doc and queues "Spec sync" items for `kaizen-review-prep` (§10)
**Related:**
- `docs/specs/agent-handoffs-and-consults.md`: Phase 0 handoffs and deliveries, Phase 1 consults, Phase 2 messages
  between conversations. Most section references below (§4.8, §8.x, §9.x) are to this spec
- `docs/specs/agent-designer.md`: palette / canvas / engine (the three roles), the `Agent` record and its open
  `Binding.kind` enum, D5 block-on-missing
- `docs/specs/agent-version-snapshots.md`: immutable published versions, which any composition must pin
- `docs/specs/shared-projects.md` §9.6: the one place a harness degrades instead of blocking
- `docs/specs/structured-extraction.md` and `docs/specs/decision-primitive.md`: the side-call fan-out that §11's
  worked example leans on instead of a sub-agent runner
- `docs/specs/workflow-runs.md`: a proposal to group handoffs into runs the user approves once (§11.5)
- *5 Multi-Agent Patterns in Strands Agents* (dev.to, AWS Heroes): the five-pattern framing used here

---

## 1. Why this exists

The long-term direction is to let authors **compose agents in the Agent Designer**: drop agents and patterns onto
a canvas and wire them together. The handoffs spec is the first piece of inter-agent work we are building, and it
makes structural choices that patterns would depend on later: where a second agent's history lives, how its
access is resolved, how its cost and stream are attributed, and what happens when it needs the user.

If those choices are made only for "a consult", each later pattern grows its own copy of them. The copies drift,
the way model access split twice before it converged on one predicate (`CLAUDE.md`, RBAC section). This doc names
the shared parts now so they are built once.

## 2. Two layers of multi-agent work

The five Strands patterns all coordinate agents **inside one execution**: one process, usually one request. The
handoffs spec coordinates agents **across conversations**, which are durable, user-visible and reviewed. These are
different layers, and a pattern can live on either.

| | In-turn orchestration (the Strands patterns) | Across conversations (handoffs, deliveries, Phase 2) |
|---|---|---|
| Unit of work | An `Agent` invocation inside the parent's turn | A whole conversation |
| Lifetime | The request (bounded by the parent's timeout) | Indefinite; survives the tab (with F1) |
| Who sees intermediate work | Nobody, unless we stream it as nested events | The user: it's an ordinary conversation in their list |
| Context between agents | Shared (Swarm), passed along edges (Graph), or a tool call's input/output | Bounded, wrapped text only: an excerpt out, a delivery back |
| Review | None in the loop | Built in: the review sheet, the composer card, Attach |
| Cost visibility | Folded into the parent turn | Each conversation has its own cost rows |
| TTFT | Anything run before the parent's first token adds to it | None; the other conversation runs separately |
| Good for | Fast routing to specialists, short fixed pipelines | Long work, work the user wants to shape, work that should be a record |

The spec's phases sit on one side each:

- **Phase 0 (handoffs) and Phase 2 (messages)** are the cross-conversation layer.
- **Phase 1 (consults)** is the in-turn layer. It's the only in-turn piece the spec proposes.

## 3. The five patterns, mapped

All five are already in our pinned dependencies. `strands-agents==1.65.0` ships `strands.multiagent` (`Swarm`,
`GraphBuilder`, `a2a`), `Agent.as_tool()`, and vended `subagent`, `graph` and `swarm` tools
(`strands.vended_tools.*`, `make_subagent` / `make_graph` / `make_swarm`, new since 1.57.2; §4.2).
`strands-agents-tools==0.8.8` ships `workflow`, `swarm`, `agent_graph`, `use_agent` and `a2a_client` tools.
**None is used in `backend/src`.** Our only A2A code is the client-side config model `A2AAgentConfig`
(`apis/shared/tools/models.py`).

| Pattern | Who decides the order | In-turn version here | Cross-conversation version here |
|---|---|---|---|
| **Agents as Tools** | The orchestrator model | Phase 1 consult, triggered by the user's `@`-mention instead of the model. Alternative A in the handoffs spec §12 (a model-called `consult_agent` tool) is the pure form | Phase 0c `suggest_handoff`: the model proposes, the user confirms |
| **Swarm** | The agents, through handoffs | Not proposed | Phase 2 `send_to_conversation`, especially 2c wake. The hop budget (§9.5) plays the role of Swarm's `max_handoffs` |
| **Graph** | The developer, through edges | Not proposed | Latent: a conversation's links are edges and deliveries carry output along them (§5) |
| **Workflow** | The developer, through task dependencies | Not proposed | Not proposed |
| **A2A** | The calling side | Client config only | A non-goal in the handoffs spec §5. Delivery is A2A's shape kept inside one platform and one user |

Notes on each:

- **Agents as Tools.** The handoffs spec deliberately does *not* build the consult as a tool, for two reasons (§8.3).
  First, the tool's spec would have to sit in `toolConfig`, the first cachePoint, and registering it only on
  mention turns would re-write the prefix. Second, Strands re-runs an interrupted tool from the top, so an OAuth
  pause inside the consult would re-run the whole consult on resume. Both problems come back for any agent-as-tool
  we write by hand, and §6.3 below is how a composed agent avoids the first. The SDK's vended `subagent` tool now
  answers the second for its own children (§4.2): it snapshots the paused child and restores it on resume instead
  of re-running it.
- **Swarm.** Strands' `Swarm` takes `max_handoffs`, `max_iterations`, `execution_timeout`, `node_timeout` and
  repetitive-handoff detection. It runs in **shared working memory**: every agent sees the team's context. Our
  cross-conversation version deliberately never shares history, only bounded wrapped text. In-turn, a swarm would
  inherit shared context, which is where it gets its token cost (§7).
- **Graph.** `GraphBuilder` supports conditional edges, cycles (with `reset_on_revisit` and
  `set_max_node_executions`), timeouts, and nesting a Swarm or Graph as a node. It is deterministic, which fits our
  cost tenet better than Swarm.
- **Workflow.** This is a *tool*, not an SDK orchestrator. Its unit is a task, not an agent. It's the least natural
  fit for a designer whose unit is the Agent, and is the lowest priority here.
- **A2A.** If we ever expose an A2A server, the `streaming=True` rule in `CLAUDE.md` applies.

The first outside workflow we were asked to host (§11) is a Graph whose nodes are single prompts, not agents. It is
the case this table covers least.

## 4. The substrate every in-turn pattern needs

Running a second `Agent` inside a turn collides with rules this platform already learned the hard way. Each one
is already answered once in the handoffs spec, for the consult. **The proposal is to build those answers as one
pattern-agnostic runner**, with the consult as its first caller.

| Need | Why it's hard here | Where the handoffs spec already answers it |
|---|---|---|
| **Where the sub-agent's history lives** | Per-session state must never be cached on an `Agent` instance, and a session id alone doesn't name a conversation (#741, #751, the `(session, user)` rule) | A hidden, deterministic sub-session per (parent, agent): `consult-<parentSessionId>-<agentId>` (§8.4) |
| **Resolving the sub-agent for this user** | Access, the published version snapshot for non-owners, RBAC-filtered tools, project-harness refusal, KB and Memory Space hydration | `resolve_turn_agent(...) -> ResolvedTurnAgent` in `inference_api/chat/routes.py`, extracted in PR-1.1 (built 2026-11-03 with PR-0.3a; §8.3). It is async |
| **What the sub-agent sees** | Full history is the most expensive option (alternative B, ~$0.21 per call on a 50k-token thread) | The excerpt renderer, watermarked (§4.5, §8.4) |
| **What comes back** | Sub-agent output is untrusted and must be bounded | A capped, wrapped report with a status (§8.5) |
| **Streaming** | The SPA builds the parent's message from raw content events | Re-tagged nested events: `consult_start` / `consult_status` / `consult_delta` / `consult_end` (§8.6) |
| **Metering** | Every call must price and roll up to the right session | Sub-session `C#` rows carrying `parentSessionId` and `consultId`, added to the parent's `totalCost` (§8.10) |
| **Interrupts** | OAuth, tool approval, `ask_user_question` and browser sign-in all pause through `PausedTurnSnapshot` and the resume route | v1 ends the consult with `needs_attention` and forwards the prompt (§8.7) |
| **Stop and timeouts** | The parent's lease covers the whole request | One lease, a heartbeat handed to whichever agent is running, `CONSULT_MAX_SECONDS` (§8.8) |

### 4.1 A sketch of the runner

```python
async def run_sub_agent(
    parent: ParentTurn,              # session, user, lease, stream sink, cost roll-up target
    agent_ref: AgentRef,             # agentId + pinned version; resolved through resolve_turn_agent
    input: SubAgentInput,            # request text + excerpt (since a watermark)
    *,
    sub_session_id: str,             # deterministic per (parent, agent, pattern node)
    limits: SubAgentLimits,          # max seconds, max output tokens, report cap
    stream_as: str,                  # the nested-event tag the SPA renders under
) -> SubAgentResult:                 # bounded text, status, cost, artifact ids, pending prompt if any
    ...
```

The consult calls it once. An agent-as-tool calls it from a tool handler. A Swarm or Graph node would be an
`Agent` built through the same `resolve_turn_agent` + `get_agent` path, so each node gets its own resolved tools,
RBAC and session manager.

### 4.2 What Strands now handles, and what it doesn't

Strands' multi-agent orchestrators have grown since the consult design was written. In our pinned 1.65.0:

- **`Swarm` and `Graph` both support user interrupts** raised from nodes (`BeforeNodeCallEvent` hooks and agent
  nodes), with their own `_InterruptState` and resume handling. The handoffs spec's "interrupts are the hardest
  unsolved part" is now partly a framework feature.
- **Both take a `session_manager`** for persisting orchestration state and execution history, plus hooks and
  plugins.
- **`Graph` and `Swarm` accept prebuilt nodes** (`Agent` / `AgentBase`, or a nested `MultiAgentBase`).
- **Vended `subagent`, `graph` and `swarm` tools** (`strands.vended_tools`, new since 1.57.2) let the *model* start
  a child agent, graph or swarm from a tool call. Each factory takes a `builder` that turns an `AgentSpec` into the
  child `Agent`, so the host decides how children are built; without one, a child inherits the parent's model and
  tools. The three share one child streamer (`multiagent/_agent_tool_streamer.py`) with `Agent.as_tool()`.
- **A paused child resumes, not re-runs.** When a child stops on an interrupt, the streamer snapshots it into the
  *parent's* `_interrupt_state.context` (`sub_agent_interrupted_turns`, keyed by `toolUseId`), and on resume the
  tool rebuilds the child from the same tool input and restores that snapshot.
- **Builders may be async** (harness-sdk#6093, in 1.65.0). A builder can return an awaitable; `subagent` awaits it
  on both the fresh and the resume path, and so do `graph` and `swarm`. Before this, a builder that had to call
  `resolve_turn_agent` (async: RBAC, the version snapshot, KB hydration, MCP pre-flight) would have needed async
  work run from a sync callback on the event loop. Now the builder can be `async def build(spec): ...` and await the
  turn's own phases, which is what CLAUDE.md requires of anything that builds the agent a turn would.
- **A child that gave no answer is a tool error** (also #6093). A child that stops with `content_filtered` or
  `refusal` comes back as `"<label> stopped before finishing (stop_reason=X)."` with any partial output, not as
  a success. This applies to `as_tool()` too. `guardrail_intervened` deliberately stays a success, because Bedrock
  also reports it for masked output. That matches our chat path: `DeclinedAnswerHook`
  (`session/hooks/declined_answer.py`) acts only when the call streamed no text. It maps onto the consult's
  `blocked` / `error` statuses (handoffs spec §8.5). One difference: the handoff brief
  (`inference_api/chat/handoff_brief.py`) falls back on `guardrail_intervened` even when text came back. That is
  the safe choice for a brief, but a runner should take the SDK's rule, not copy the brief's.

What remains ours to solve:

- ⚠️ **Verify first:** how a multi-agent `session_manager` coexists with `TurnBasedSessionManager` on each node, and
  whether a node's history restores byte-stable between turns. That property is the prompt-cache contract.
- ⚠️ **Verify first:** whether a Swarm/Graph interrupt can ride our `PausedTurnSnapshot`, `PendingInterrupt`
  breadcrumbs and resume route without a new `kind`, or needs one (the handoffs spec §8.7's "v2"). For the vended
  tools this question is narrower. Our session manager already persists the parent's `_interrupt_state` to
  AgentCore Memory, so the child's snapshot would travel with it. What's left to check is the snapshot's size (it
  is the child's whole state, on top of the parent's) and whether a child's re-prefixed interrupt id
  (`<namespace>:<toolUseId>:<child id>`) survives our `PendingInterrupt` breadcrumbs, the extractors that read a
  breadcrumb's `kind` from the interrupt, and the resume guard's id check.
- The rest of §4's table: access, metering, streaming and cost are platform concerns no framework supplies.

## 5. The cross-conversation layer as a pattern host

The handoffs spec's durable layer is not just a delivery mechanism. Read as a pattern host:

- **Links + deliveries are a graph of conversations.** A conversation's links (its `spawnedFrom` parent and its
  handoff targets) are edges; a delivery carries output along an edge (§4.8). A developer-defined Graph on this
  layer would be a **durable pipeline**: each node is a real conversation the user can open, edit, stop or
  continue, and each step's cost is its own.
- **Phase 2 is a swarm on this layer.** Agents decide when to message each other; the hop budget and per-hour caps
  bound it; pending-first landings mean nothing runs unattended unless the deployment turns on 2c.
- **Scheduled runs are a trigger for it.** A schedule that delivers into an existing conversation, instead of
  opening a new one, is a pipeline stage that runs on a clock (F2's missing case, noted in §4.8).

**When to put a pattern here instead of in-turn:** each step is long (minutes, not seconds); the user should see or
shape intermediate output; a step needs the user (consent, an answer) and shouldn't hold the whole request open;
or the run must survive the tab. The cost is latency between steps and more visible surface, so short fixed
pipelines belong in-turn.

## 6. The Agent Designer, long term: composing agents and patterns

The Designer spec already splits the job into three roles. Composition extends each one.

### 6.1 Palette: agents become a bindable primitive

`Binding.kind` is an open enum (`'knowledge_base' | 'tool' | 'skill' | 'memory_space' | …`). The smallest step to
composition is a new kind:

```
Binding { kind: 'agent', ref: <agentId>, config: { version: <pinned>, role: 'tool' | 'consult', limits } }
```

That is agents-as-tools as a binding. A pattern ("Swarm", "Graph") would be a second palette entry: a block that
holds agents instead of an agent that holds primitives.

### 6.2 Canvas: from a form to a graph

Today the canvas is a form over one `Agent` record. Composition makes it a graph editor. One possible record:

```
Composition {
  pattern:  'agents_as_tools' | 'swarm' | 'graph'
  nodes:    [{ nodeId, agentRef: { agentId, version }, role? }]
  edges:    [{ from, to, condition? }]          # graph only
  entry:    nodeId
  limits:   { maxHandoffs?, maxNodeExecutions?, executionTimeout, nodeTimeout, maxCostUsd? }
  layer:    'in_turn' | 'conversations'         # §2: where it runs
}
```

Drag-and-drop is the authoring UI for this record, not the record itself. Following Designer D6 ("ship
consumption first; the Designer follows"), the first version can be a form, "Add an agent as a tool", long before
a canvas exists.

*2026-11-20:* the first canvas proposed is for the cross-conversation layer, not this record:
`docs/specs/workflow-designer.md`. Its graph arranges itself from what each step reads. If this record gets a
canvas, it should reuse that component and that rule.

### 6.3 Engine: the runner, plus the rules a composition must keep

- **Pin versions.** A composed agent references published version snapshots of its nodes, never live drafts. An
  owner editing a node agent must not silently change every composition that uses it, or change the composition's
  cache key and its approval.
- **Keep the orchestrator's `toolConfig` deterministic.** Sub-agent tool specs come from the pinned versions, in a
  stable order, and change only when a new composition version is published. That keeps the prompt-cache contract
  for agent-as-tool.
- **Runnability is all-or-nothing by default.** Designer D5 blocks a run when a binding is missing; a composition
  is runnable for a user only if every node is. Shared Projects' degrade rule (§9.6) is the one exception, and
  whether it should extend to compositions is open (§9).
- **Limits are part of the record.** A Swarm without `max_handoffs` or a Graph cycle without
  `max_node_executions` has unbounded cost. The Designer should not save a composition without them, and should
  show an estimate from the node models' rates.
- **Approval covers the whole composition.** Marketplace approval of a composed agent approves its pinned nodes and
  its limits together.
- **Every node is metered and attributable.** `C#` rows name the composition, the node and the pinned version, so
  the cost drill-down can say which node spent what.

## 7. Cost and latency by pattern

No measured numbers yet. These are the directions to measure before any pattern ships, per the token cost tenet.

| Pattern | Token cost drivers | Determinism | TTFT effect if in-turn |
|---|---|---|---|
| Agents as Tools | One orchestrator call plus each sub-agent call; sub-agent prefixes cache per sub-session | Model-routed | Small: the orchestrator can stream first and call later |
| Swarm | Shared context grows with every handoff; each agent re-reads it; handoff reasoning | Low; paths differ between runs | Large if the first node isn't the one that answers |
| Graph | Each node reads only its inputs; no routing reasoning | High | Grows with the depth before the first streaming node |
| Workflow | Per task; parallel tasks overlap wall-clock | High | As Graph |
| Cross-conversation | Each conversation's own prefix; a woken idle conversation pays a cold cache write (handoffs spec §9.7) | Depends on the pattern | None |

Nested status events (the `consult_status` pattern) help perceived latency while sub-agents run, but they don't
change the TTFT budget. Any in-turn pattern that runs before the parent's first token needs its number stated in
the PR and accepted explicitly, as `CLAUDE.md` requires.

## 8. Suggested sequence

Nothing here should jump the handoffs spec's gates. The point is to make each step reusable by the next.

1. **Handoffs Phase 0** as specced. It is the cross-conversation layer and the measurement.
2. **PR-1.1, the `resolve_turn_agent` extraction.** *Done* (2026-11-03, with PR-0.3a, which needed it for the
   handoff brief). Every in-turn pattern builds its agents through it.
3. **If Phase 1 passes its gate:** build §4's runner pattern-agnostically, with the consult as its first caller.
   Settle §4.2's two verify-firsts while doing it.
4. **Agents as tools as a Designer binding** (`kind: 'agent'`, §6.1), on the runner. This is the first composition
   a user can author, and it can be a form.
5. **Graph, then Swarm, in-turn**, once nested interrupts ride our resume path and the cost of a node is visible in
   the drill-down. Graph first because it's deterministic.
6. **Durable pipelines on the cross-conversation layer** (§5), if Phase 2 shows linked conversations get used.
7. **The drag-and-drop canvas**, when there are enough composition kinds that a form stops being enough.

Outside this sequence: a fan-out of prompt nodes over one document (§11.5) uses the side-call engine, not the
runner, so it can be built before or after any step above.

## 9. Open questions

1. **Layer per composition, or per node?** Can one composition mix in-turn nodes and conversation nodes (a Graph
   whose last node is a handoff), or does each composition pick a layer?
2. **Degrade or block?** Should a composition shared with someone missing one node's tool degrade like a project
   harness, or block like every other agent (Designer D5)?
3. **Who may compose whose agents?** May an author bind someone else's published agent as a node? If so, whose
   instructions does the excerpt reach, and does the node owner see that their agent is used?
4. **Skills vs. agent nodes.** The Designer spec already asks whether Skills become reusable binding bundles. A
   composed node is a third candidate for the same job. Do we need all three?
5. **One runner, or the Strands orchestrators directly?** Build our own runner for agent-as-tool and use
   `Swarm` / `GraphBuilder` for the rest with our nodes, or route everything through the runner for one metering
   and streaming path? Since 1.65.0 there is a middle option: the vended `subagent` / `graph` / `swarm` tools with
   an async `builder` that calls `resolve_turn_agent` (§4.2). That gets resume-not-re-run and the no-answer error
   from the SDK. Streaming, metering and the sub-session would still be ours, and a model-called tool still puts
   its spec in `toolConfig` (§3, Agents as Tools).

   **Leaning (2026-11-16), to settle at the start of Phase 1:** for delegation the *model* starts (§8 steps 4 and
   5), build on `make_subagent` / `make_graph` / `make_swarm` with an async builder instead of a child runner of
   our own. §4's runner shrinks to the platform half: the builder (`resolve_turn_agent` + `get_agent`), the
   sub-session and its metering, re-tagging `ToolStreamEvent`s into nested events, and the report cap. Four
   settings are not optional:

   - **The `context` axis is `Fixed("none")`.** The task carries our token-bounded excerpt. `all` forks the
     parent's messages (alternative B's cost), and `last_messages` counts messages, not tokens.
   - **Presets and axes are constant per deployment.** The tool's schema is derived from them, so a preset list
     built from a user's agents would rewrite `toolConfig` whenever it changed.
   - **The child's answer is capped.** The tool returns `str(result)`, the whole final answer, so the cap and
     wrapper of handoffs spec §8.5 still apply.
   - **No-answer and paused children map onto the report statuses** (§8.5), so both paths say the same thing.

   The consult stays its own small runner, because a user-triggered call is not a tool call and the SDK's child
   streamer (`multiagent/_agent_tool_streamer.py`) is private. It shares the builder and the statuses, so the two
   paths agree. Not now: Phase 1 hasn't passed its gate, and the SDK side is still moving (harness-sdk#6088 is
   open, `PresetOptions` is labelled `api/needs-review`). At the start of Phase 1, a dev spike with
   `make_subagent` and an async builder settles it, by confirming four things: the child's snapshot fits in
   AgentCore Memory; a child interrupt id gets through our breadcrumbs and the resume guard; `toolConfigHash`
   holds between turns; and every child call writes a priced `C#` row.
6. **Cost ceilings.** Should a composition carry a per-run `maxCostUsd` enforced by the runner, on top of the
   user's quota?
7. **Are prompt nodes a composition, or a tool?** §11's graph needs no agent per node. Is a prompt fan-out a
   `prompt` node kind in the `Composition` record (§6.2), authored on the canvas, or only a panel in a skill's
   references, authored like a field set, with no composition record at all? The second is far less to build. The
   first is what lets an author mix prompt nodes and agent nodes in one graph.

## 10. Keeping this current

This doc is a living plan, not a record of a decision, so it rots fastest where it describes things outside our
control. The weekly `kaizen-research` run checks it (standing watch 2b in its skill) and `kaizen-review-prep` puts
the results in a "Living Spec Sync" block of the review. A sync lands as its own small docs PR, like any kaizen
idea; the kaizen skills never edit specs directly.

The claims most likely to go stale, and what makes each one stale:

| Claim | Section | Stale when |
|---|---|---|
| Pinned `strands-agents==1.65.0` / `strands-agents-tools==0.8.8` | §3, §4.2 | A pin bump merges |
| No multi-agent module is used in `backend/src` | §3 | Any import of `strands.multiagent`, `strands.vended_tools.{subagent,graph,swarm}`, `as_tool()` or the multi-agent tools lands |
| Swarm and Graph support interrupts, `session_manager` and prebuilt nodes | §4.2 | A Strands release changes `strands/multiagent/` |
| Vended `subagent` / `graph` / `swarm` take a sync-or-async `builder`, resume a paused child from a snapshot in the parent's interrupt state, and report `content_filtered` / `refusal` children as tool errors | §4.2, §9 | A Strands release changes `strands/vended_tools/` or `multiagent/_agent_tool_streamer.py` |
| The two verify-firsts are open | §4.2 | Either is settled, by us or upstream |
| Handoffs, deliveries, consults and messages are unbuilt | §2, §3, §5 | Any of their PRs merge (the handoffs spec's §11 lists them) |
| `Binding.kind` has no `'agent'` value | §6.1 | A composition binding lands in the Agent Designer |
| The cost and TTFT directions in §7 are unmeasured | §7 | Any pattern is measured in dev |
| `document_extract` takes one field set per call and is not in `PARALLEL_SAFE_TOOLS` | §11.4 G1 | Its signature or that set changes |
| The decision primitive is a draft, and its `document` adapter starts from a 24,000-character cap | §11.4 G2 | A decisions PR merges, or `decision-primitive.md` §6.2 is revised |
| No prompt-panel fan-out exists, and no builtin tool writes a PDF | §11.3, §11.4 G3 and G4 | Either lands |
| An external MCP tool has no way to receive or return a conversation's file | §11.4 G5 | The verify-first is settled, or a file hand-off to MCP servers lands |

When implementation starts on anything in §8, update this doc in the same PR as the code, as the handoffs spec does
for its own phases. The kaizen watch is the backstop, not the primary mechanism.

## 11. Worked example: a fixed fan-out over one document (2026-11-19)

The first outside workflow we were asked to host. A student collaborator built a research-proposal reviewer on
LangGraph. It runs on a laptop, takes a proposal PDF, returns a marked-up PDF, and is traced in Langfuse. They want
it usable from chat. It is recorded here because it tests §3's map against something an author built without us.
Nothing in this section is scheduled.

### 11.1 The graph

| Stage | What it does | Fan-out |
|---|---|---|
| `ingest` | Turns the PDF into numbered text | — |
| Review lane | Four reviewer personas read the whole proposal, on two model tiers | 4 in parallel |
| `review_summary` | One call merges the four reviews into inline notes | fan-in |
| `pull_criteria` | Loads the audit criteria from JSON | — |
| Audit lane | One branch per audit section (LangGraph `Send`): 15 sections of 4 to 13 pass/fail criteria, 114 in all | 15 in parallel |
| `aggregate_audits` | Collects pass or fail per criterion | fan-in |
| `build_review` | Writes the marked-up PDF from both lanes (the audit results are not in the report yet) | — |

### 11.2 What it shows about §3

- **The developer sets the order.** All 19 branches run on every proposal, and no model chooses among them. That
  is Graph or Workflow, the two rows §3 marks "not proposed" in-turn and §8 puts fifth. Agents as Tools, which §8
  builds first, cannot express it.
- **The branches look like prompts, not agents.** As drawn, each one is an instruction plus the same document in,
  and a bounded answer out: no tool loop, no history, no question for the user. Built as an `Agent` per branch
  through `resolve_turn_agent`, each would get a sub-session, a session manager and an interrupt path it never
  uses. §4's substrate is the wrong weight for this. ⚠️ **Verify first:** the author describes RAG and tool use
  somewhere in the pipeline. Confirm which nodes, if any, loop on tools before relying on this.
- **The fan-out is over instructions, not documents.** One document meets many instruction sets. Our one built
  fan-out, `document_extract`, runs the other way: one field set over N documents.
- **The result is a file marked at positions**, not a chat answer.
- **The cross-conversation layer does not fit.** A handoff is started by the user, one at a time; a send carries at
  most three deliveries; every step waits on a button. That review is the point of the layer (§2), and it is wrong
  for 19 unattended branches that belong to one run.

### 11.3 Node by node

| Their node | Closest thing here | State |
|---|---|---|
| `ingest`, retrieval | Attachments, `document_read`, a project knowledge base | Shipped |
| Audit branch | `document_extract` with one field set per audit section: a typed value per criterion with a quote, a locator and a `source_provenance` state. Every section fits `MAX_FIELDS = 14` (`apis/shared/extraction/field_sets.py`); the largest has 13 | In development (`DOCUMENT_EXTRACT_ENABLED`), with gaps G1 and G2 |
| `aggregate_audits` | The extraction table and workbook | Same |
| Reviewer branch | Nothing. One agent can play the four personas in sequence in a turn, which loses their independence, the per-branch model and the parallel wall-clock | Gap G3 |
| `review_summary` | The turn's own answer; the reduce is markdown in a skill (`structured-extraction.md` §17.3) | Shipped |
| `build_review` | No builtin tool writes a PDF. `redline_word_document` is the nearest output | Gap G4 |

### 11.4 Gaps

- **G1. Several field sets over one document.** `document_extract(upload_ids, field_set, pages?)` takes one set. The
  audit is 15 sets over one document, so today it is 15 tool calls. They would not overlap even with
  `CONCURRENT_TOOL_EXECUTION_ENABLED` on, because `document_extract` is not in `PARALLEL_SAFE_TOOLS`
  (`agents/main_agent/core/tool_concurrency.py`), and each call writes its own artifact and workbook. Merging the
  sets is not an option: 114 fields is far past the 14 Bedrock will compile.
  *2026-11-22:* `workflow-designer.md` §22 plans checklist steps that run several field sets on one step, one call
  per set over a shared cached document, which answers this for workflows without changing `document_extract`.
- **G2. A verdict needs a third answer.** An audit criterion is pass, fail or "cannot tell", with evidence. A
  `boolean` field has no way to decline, and its provenance state is `unassessed` by construction, since a yes/no
  cannot be matched against a passage. `Uncertain` in `decision-primitive.md` is the right answer, but that spec
  is a draft built around many items and a few questions. Its `document` adapter starts from a 24,000-character
  cap (§6.2 there, to be measured), which is smaller than a full proposal. This case is one item and 114
  questions.
- **G3. A prompt panel.** N free-text instructions over one document, with bounded concurrency, a model per branch
  and one reduce. Nothing here does it. A sketch that reuses what exists: a named panel in a skill's reference
  bundle, authored like a field set; one tool whose spec is constant per deployment, so `toolConfig` never moves;
  side calls through extraction's `run_bounded`, each writing a priced `C#` row under its own `callKind`; bounded
  outputs returned to the turn's model, which does the reduce. ⚠️ **Measure first:** whether the branches read the
  document from a shared cached prefix (document first, one call warmed before the rest, as extraction warms a
  grammar). Caches are per model, so two tiers pay two writes. Without that, each of 19 branches pays for the whole
  proposal at the uncached rate. *Measured 2026-11-21* (`workflow-designer.md` §21.1): with the document first, a
  cachePoint after it and one warm call, 16 of 16 branches read it on Haiku 5.5 and Sonnet 5.5, for 3.6–3.9× less
  input; in a turn's layout none did. Prompt steps are being built as that side call (§21.3 there).
- **G4. An annotated PDF.** A `source_provenance` locator (`kind`, `value`, `precision`) plus its quote is the
  anchor an annotator needs, and nothing consumes it for output yet. Skill scripts could carry the annotator, but
  they run only under the Word tools today (`WORD_SKILL_SCRIPTS_ENABLED`).
- **G5. Hosting an orchestrator we did not build.** The path that exists is an external MCP server in the tool
  catalog, with the whole pipeline behind one tool. ⚠️ **Verify first:** how such a tool receives the conversation's
  uploaded file and returns one. Nothing in `agents/main_agent/integrations/` hands a file to an MCP server today.
  The MCP server config has no per-call timeout field, so a run of about 20 model calls needs a start-then-poll
  pair of tools, not one long call. A2A is still client config only (§3). The author also loses per-node traces:
  we export nothing to Langfuse, and `C#` rows cover only calls the platform makes.
- **G6. Run length.** Unmeasured. If a full run does not fit a turn comfortably, the durable form is §5's: a
  headless run (`run_agent_headless`) that delivers into the conversation.

### 11.5 What this suggests

- **A second kind of node.** §6.2's `Composition` assumes every node is an agent. A `prompt` node kind, run on the
  side-call engine (`extraction/batch.py` today, `decisions/batch.py` once that spec lands), would cover this graph
  without §4's runner. It needs neither of §4.2's verify-firsts, so it does not wait on the Phase 1 gate (§9 Q7).
- **Reviewers as Agents, under one approval.** `docs/specs/workflow-runs.md` (proposal, 2026-11-19) takes the
  other route for the review lane: each reviewer is an Agent in a conversation the user can continue, several
  handoffs start from one review sheet, and their results return as one card. It keeps prompt and checklist nodes
  as cheaper step kinds, so the two designs meet in one record. If it is accepted, it is where §5's durable
  pipeline gets built.
- **Order for this collaboration.** First, host the pipeline as it is behind an external MCP tool (G5). Second,
  rebuild the audit lane as 15 field sets on dev and score it against the graph's own output, which is a ready
  baseline. Third, let that comparison decide which of G1 to G4 are worth building.
