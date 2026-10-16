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

All five are already in our pinned dependencies. `strands-agents==1.57.2` ships `strands.multiagent` (`Swarm`,
`GraphBuilder`, `a2a`), and `strands-agents-tools==0.8.8` ships `workflow`, `swarm`, `agent_graph`, `use_agent` and
`a2a_client` tools. **None is used in `backend/src`.** Our only A2A code is the client-side config model
`A2AAgentConfig` (`apis/shared/tools/models.py`).

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
  pause inside the consult would re-run the whole consult on resume. Both problems come back for any agent-as-tool,
  and §6.3 below is how a composed agent avoids the first.
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

## 4. The substrate every in-turn pattern needs

Running a second `Agent` inside a turn collides with rules this platform already learned the hard way. Each one
is already answered once in the handoffs spec, for the consult. **The proposal is to build those answers as one
pattern-agnostic runner**, with the consult as its first caller.

| Need | Why it's hard here | Where the handoffs spec already answers it |
|---|---|---|
| **Where the sub-agent's history lives** | Per-session state must never be cached on an `Agent` instance, and a session id alone doesn't name a conversation (#741, #751, the `(session, user)` rule) | A hidden, deterministic sub-session per (parent, agent): `consult-<parentSessionId>-<agentId>` (§8.4) |
| **Resolving the sub-agent for this user** | Access, the published version snapshot for non-owners, RBAC-filtered tools, project-harness refusal, KB and Memory Space hydration. Today ~450 lines inline in `chat/routes.py` | `resolve_turn_agent(...) -> ResolvedTurnAgent`, extracted in PR-1.1 (§8.3) |
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

Strands' multi-agent orchestrators have grown since the consult design was written. In our pinned 1.57.2:

- **`Swarm` and `Graph` both support user interrupts** raised from nodes (`BeforeNodeCallEvent` hooks and agent
  nodes), with their own `_InterruptState` and resume handling. The handoffs spec's "interrupts are the hardest
  unsolved part" is now partly a framework feature.
- **Both take a `session_manager`** for persisting orchestration state and execution history, plus hooks and
  plugins.
- **`Graph` and `Swarm` accept prebuilt nodes** (`Agent` / `AgentBase`, or a nested `MultiAgentBase`).

What remains ours to solve:

- ⚠️ **Verify first:** how a multi-agent `session_manager` coexists with `TurnBasedSessionManager` on each node, and
  whether a node's history restores byte-stable between turns. That property is the prompt-cache contract.
- ⚠️ **Verify first:** whether a Swarm/Graph interrupt can ride our `PausedTurnSnapshot`, `PendingInterrupt`
  breadcrumbs and resume route without a new `kind`, or needs one (the handoffs spec §8.7's "v2").
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
2. **PR-1.1, the `resolve_turn_agent` extraction**, whenever the chat route is next touched. It's a pure refactor,
   it doesn't depend on any gate, and every in-turn pattern needs it.
3. **If Phase 1 passes its gate:** build §4's runner pattern-agnostically, with the consult as its first caller.
   Settle §4.2's two verify-firsts while doing it.
4. **Agents as tools as a Designer binding** (`kind: 'agent'`, §6.1), on the runner. This is the first composition
   a user can author, and it can be a form.
5. **Graph, then Swarm, in-turn**, once nested interrupts ride our resume path and the cost of a node is visible in
   the drill-down. Graph first because it's deterministic.
6. **Durable pipelines on the cross-conversation layer** (§5), if Phase 2 shows linked conversations get used.
7. **The drag-and-drop canvas**, when there are enough composition kinds that a form stops being enough.

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
   and streaming path?
6. **Cost ceilings.** Should a composition carry a per-run `maxCostUsd` enforced by the runner, on top of the
   user's quota?

## 10. Keeping this current

This doc is a living plan, not a record of a decision, so it rots fastest where it describes things outside our
control. The weekly `kaizen-research` run checks it (standing watch 2b in its skill) and `kaizen-review-prep` puts
the results in a "Living Spec Sync" block of the review. A sync lands as its own small docs PR, like any kaizen
idea; the kaizen skills never edit specs directly.

The claims most likely to go stale, and what makes each one stale:

| Claim | Section | Stale when |
|---|---|---|
| Pinned `strands-agents==1.57.2` / `strands-agents-tools==0.8.8` | §3 | A pin bump merges |
| No multi-agent module is used in `backend/src` | §3 | Any import of `strands.multiagent` or the multi-agent tools lands |
| Swarm and Graph support interrupts, `session_manager` and prebuilt nodes | §4.2 | A Strands release changes `strands/multiagent/` |
| The two verify-firsts are open | §4.2 | Either is settled, by us or upstream |
| `resolve_turn_agent` doesn't exist yet | §4, §8 | PR-1.1 merges |
| Handoffs, deliveries, consults and messages are unbuilt | §2, §3, §5 | Any of their PRs merge (the handoffs spec's §11 lists them) |
| `Binding.kind` has no `'agent'` value | §6.1 | A composition binding lands in the Agent Designer |
| The cost and TTFT directions in §7 are unmeasured | §7 | Any pattern is measured in dev |

When implementation starts on anything in §8, update this doc in the same PR as the code, as the handoffs spec does
for its own phases. The kaizen watch is the backstop, not the primary mechanism.
