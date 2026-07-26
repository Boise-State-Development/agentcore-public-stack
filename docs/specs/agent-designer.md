# Agent Designer — a unified authoring surface for composed Agents

**Status:** Draft / proposal (2026-07-07)
**Author:** (drafted with Claude)
**Targets branch:** `develop`
**Supersedes:** the "extend the Assistant editor to bind a Memory Space" assumption in
[`user-markdown-memory.md`](user-markdown-memory.md) §B1.
**Related:** agentic-platform primitives epic F5 (memory) + F6 (registry/governance)
([`agentic-platform-primitives.md`](agentic-platform-primitives.md)); admin Skills + RBAC + tool
binding ([`admin-skills-rbac-tool-binding.md`](admin-skills-rbac-tool-binding.md)); managed-Harness
adopt-with-boundary spike ([`docs/kaizen/`](../kaizen/)); assistant sharing / collaborative editing
(issue #113); model access control (`apis/app_api/admin/services/model_access.py`); Memory Spaces
(`apis/shared/memory/`).

## Summary

Give users a single, first-class surface — the **Agent Designer** (the **Agent Harness Editor**) —
where they **compose an Agent** from the platform's governed primitives: system instructions, a
**model**, knowledge bases, tools, skills, **Memory Spaces**, and whatever primitives come next. The
user only ever sees the primitives (and models) **their role enables**.

This is the UX capstone of the agentic-primitives epic. We have spent several phases shipping
**bindable primitives** — tools, skills, KBs, and now Memory Spaces — without ever building the thing
that **binds** them. Today "an agent" is approximated by two overlapping, partial records:
**Assistants** (instructions + one KB + sharing) and **Skills** (instructions + reference files +
bound tools). Neither composes the full primitive set, and Memory Spaces wanting to be "the 4th
bindable primitive" has no unified record to be the 4th binding *of*. The Agent Designer is that
record and that surface.

The Agent **replaces the term and feature "Assistant."** The Designer is built as a **new page,
separate from the existing Assistants editor**; Assistants are retired gracefully once the Designer
reaches parity.

---

## Terminology (industry-aligned, and internally precise)

A "harness," in common usage and in AgentCore's own API (`CreateHarness`), is the **runtime** — the
scaffolding that runs the agent loop, dispatches tools, manages context. It is *not* the design
canvas. We keep the terms crisp:

| Term | Role |
|------|------|
| **Agent** | The product concept (replaces "Assistant"): persona + model + bound capabilities. What a user creates and runs. |
| **Agent Harness** | The configured, runnable **definition** the runtime executes — bindings + run params. Matches AgentCore's `CreateHarness` artifact. |
| **Agent Designer** / **Agent Harness Editor** | The authoring UI. You *edit the harness definition*; the Harness (runtime) executes it. |
| **Registry** | The palette/catalog of what is bindable, filtered by role. |

Our existing spec already names Workstream B "Agent / **Harness** consumption" — so "harness = the
runtime that consumes bindings and runs the loop" is already house usage. The Designer edits what the
harness runs.

## The three roles (why "binding" needs its own layer)

"Designing an agent" decomposes into three distinct jobs we have been conflating:

- **The palette** — *what can I bind?* Enumerate available tools / skills / KBs / memory spaces /
  models, filtered by role. **This is the Registry.**
- **The canvas** — *the composed agent itself:* instructions + model + bound primitives. A **record**.
  Neither Harness nor Registry.
- **The engine** — *resolve the bindings and run the loop.* **This is the Harness** (inference-api
  today).

Binding is **authored on the canvas, governed by the Registry, resolved by the Harness.** The middle
layer — the Agent record with a uniform binding model — is the thing that has been missing.

---

## Core design decisions

### D1 — Own the Agent contract; federate AWS, don't build on it

We do **not** build the composition surface on **AgentCore Registry**. Our bindable set is broader
than AgentCore natively catalogs (it indexes AgentCore-native runtimes/gateway tools; our palette
includes **Memory Spaces, app-level Skills, our KBs/vector indexes**). Forcing those into AWS's schema
is a poor fit and a lock-in to a moving target. This mirrors the **managed-Harness spike finding**
(*adopt-with-boundary*: customParameters pinning worked, but the managed Gateway 3LO path was broken,
so we kept our own token path).

**We own a primitive-agnostic `Agent` contract and an internal catalog.** AgentCore Registry (and the
managed Harness) become **optional federated sources** behind the catalog later — the same "dynamic
discovery tier" idea already in the tool-search strategy. Optionality without lock-in.

### D2 — Evolve the assistant store into the Agent store (no parallel table)

The Agent **UI** is new; the Agent **data** evolves the existing assistant store (the `rag-assistants`
table) in place, adding the binding structure. Rationale: sharing/collab already works there, and a
parallel table with dual-write is migration risk we don't need. An existing Assistant **is** an Agent
whose bindings are `{ instructions, knowledge_base }`. A compatibility read-mapping presents legacy
assistants as Agents until backfilled; the new Designer writes the full shape.

### D3 — Uniform binding model; the model is a governed *single-select*, not a binding

Every capability is "just another binding," so future primitives slot in without bespoke fields. But
the **model** is singular and required (an agent runs on exactly one) — it stays a distinct field, not
a member of the additive `bindings[]` array (a required singleton in an array is how you get
"saved with 0 or 2 models" bugs). It flows through the **same catalog + RBAC machinery** for
discovery.

### D4 — RBAC = compose existing per-primitive access, don't rebuild it

Each primitive already owns its access rule; the catalog API's whole job is to **union and filter**:

| Primitive | Existing access check |
|-----------|-----------------------|
| Model | `ModelAccessService.can_access_model` (`allowed_app_roles` per `ManagedModel`) |
| Tool | tool RBAC / cohort capabilities (`apis/app_api/tools/`) |
| Skill | admin-skills RBAC (`accessible_skill_ids`) |
| Knowledge base | assistant KB access |
| Memory Space | `MemorySpaceService.resolve_permission` (identity-based) |

Five confirmations that we invent **no new access system** — we compose five that exist.

### D5 — Design-time filter, run-time re-resolution, block-on-missing (v1)

Filtering the picker is authoring UX. Because Agents are **shared**, every gated capability must also
be **re-resolved at run time against the *invoking* user**, not the author. If Alice (Opus access)
builds an Agent on Opus and shares it with Bob (no Opus), the Harness resolves `modelConfig` against
*Bob's* grants at invocation. **v1 policy: block with a clear message** when an invoker lacks a
capability the Agent requires (model, memory space, or restricted tool); **downgrade** is a later
opt-in. One rule for all gated bindings.

### D6 — Ship consumption first; the Designer follows

The memory-consumption payoff (Workstream B: an Agent reads/writes a bound space) depends only on the
**binding model + harness resolution**, not on the full Designer. We ship a **thin vertical slice**
first (one Agent, memory binding, harness reads it), then the broad Designer — protecting value
delivery and de-risking a multi-month UI build.

---

## Data model

```
Agent {
  agentId          # evolves assistantId; legacy ids remain valid
  ownerId
  name, description, instructions
  emoji?, starters[], tags[]           # carried from Assistant
  modelConfig: { modelId, params }     # required, single-select, RBAC-gated (D3)
  bindings: Binding[]                  # plural, optional (D3)
  visibility, sharedWith: ShareEntry[] # reuse assistant sharing + collab (#113)
  status, usageCount, createdAt, updatedAt
}

Binding {
  kind: 'knowledge_base' | 'tool' | 'skill' | 'memory_space' | <open enum>   # D3/D4
  ref:  <primitive id>
  config: { ... }   # kind-specific: memory_space → { access, alwaysLoad }; tool → { enabledTools[] }; …
}
```

**Back-compat mapping (D2):** a legacy Assistant reads as
`{ modelConfig: <its default model>, bindings: [ {kind:'knowledge_base', ref: vectorIndexId} ] }` plus
its instructions — no data loss, no flag day.

## APIs / contracts

- **Agent CRUD** — `/agents/*` (evolves `/assistants/*`; the assistants routes remain as a
  compatibility alias until deprecation). SPA-facing, `Depends(get_current_user_from_session)`.
- **Bindable-primitives catalog** — `GET /agents/bindable?kind=model|tool|skill|knowledge_base|memory_space`
  → RBAC-filtered list for the caller. **The palette and the RBAC enforcement point (D4).** Fans out to
  each primitive's existing list+access service and returns the union, filtered. AgentCore Registry
  becomes one more source here later (D1).
- **Binding resolution** — the **Harness** (inference-api) reads an Agent's `bindings` + `modelConfig`
  at invocation, **re-resolves each against the invoking user (D5)**, and hydrates: memory index
  injection + `memory_*` tools (Workstream B), model selection, KB retrieval, tool exposure.

Cross-package contract: backend route shapes define the API; the SPA's Agent + Binding TS interfaces
must match; SSE + binding contracts are coordinated changes.

---

## Frontend — the Agent Designer

A **new page**, separate from the Assistants editor. Layout: persona/instructions + a **model picker**
and a set of **binding pickers** (KBs, tools, skills, Memory Spaces), each populated from
`GET /agents/bindable?kind=…` so **the user only sees what their role enables (D4)**. Reuses the
`redesign-tokens` list-page idiom and `@angular/cdk/dialog` conventions; reuses the assistant
share/collab component for Agent sharing. "Agent Harness" terminology where it clarifies the runnable
definition.

## Migration / Assistant deprecation

1. ✅ Evolve the store (D2) + ship the Agent contract with the compat mapping.
2. ✅ Build the Designer to parity, then past it (tools/skills/memory binding old Assistants never had).
3. ✅ Render legacy Assistants as Agents; redirect the Assistants editor to the Designer.
4. 🔄 Deprecate the "Assistant" term across UI + docs; retire the old editor.

No big-bang: legacy ids and the compat mapping keep everything running throughout.

**Step 3, as shipped.** `/assistants`, `/assistants/new` and `/assistants/:id/edit` are
`redirectTo` entries onto their `/agents` equivalents rather than deletions. Those paths are in
bookmarks, in the "edit" link of every old chat session, and in links people shared with each
other; because the ids are the same record on both sides, the redirect lands on exactly what the
old URL opened. The sidenav ships one entry, and the Agents "Preview" badge came off with the
second noun it existed to disambiguate.

⚠️ **This changed what the `AGENTS_API_ENABLED` kill switch means.** While both nouns shipped,
turning it off degraded to the Assistants editor. There is no longer anything to fall back to, so
off now means no authoring surface at all. It is an outage switch, not a feature toggle — records
are untouched either way.

**The term pass is deliberately not a find-and-replace.** `"You are a helpful assistant that…"`
stays as the instructions placeholder: that is the conventional system-prompt idiom, and rewriting
it to "agent" would be worse prompt guidance, not better terminology. What changed is the words
naming *our product concept* — nav, the session indicator, the share dialog, settings copy.

---

## Phasing

```
Phase 0  This spec — contracts, term map, AWS-federation decision            ✅ done (#590)
Phase 1  Agent record + uniform binding model + compat mapping (back-compat)  ✅ done (#591, #592, + flag plumbing)
Phase 2  Bindable-primitives catalog API (Registry-lite, RBAC-composed)       ✅ done
Phase 3  Harness resolution: memory index injection + memory_* tools + model  ✅ done
Phase 4  Agent Designer page (Agent Harness Editor)                           ✅ done
Phase 5  Assistant deprecation + migration                                    🔄 in progress (#746)
Later    Federate AgentCore Registry / managed Harness as catalog+run backends (D1)
```

**Phase 1 status (implemented).** The Agent contract (`modelConfig` + uniform `bindings[]`),
the D2 compat mapping (legacy Assistant → Agent, KB binding synthesized on read), design-time
`binding_validation` composing the existing per-primitive RBAC checks (D4), and the governed
`/agents/*` surface (dark behind `AGENTS_API_ENABLED`, default off) all landed. Two Phase-1
refinements vs this spec's sketch: (a) `knowledge_base` bindings are **synthesized on read but
rejected on write** — the KB index isn't user-configurable and the agent id doesn't exist at
create time, so an author-settable KB binding is meaningless until F4; (b) `modelConfig` is
**optional in storage** (absent = resolve the model exactly as today) and required only at
Designer write-time — no legacy assistant carries a model, so a "required singleton" invariant
would force a fabricated backfill. `tool`/`skill` binding kinds are enum-valid but **inert**
(stored, not resolved) until Phase 2/3. **The live Oliver dogfood (D6) is gated on Phase 3
harness resolution + Memory Spaces being deployed to the target environment** — the binding is
storable now but nothing consumes it at invocation yet.

**Relationship to Workstream B (memory spec):** B's binding (`memorySpaces` declarative config) now
lands as a `memory_space` **Binding** on the Agent's uniform model rather than a bespoke field. B1 =
Phase 1's memory binding kind; B2/B3 (read/write tools + index injection) = Phase 3's harness
resolution. The memory epic's §B1 "extend the Assistant" line is superseded by this spec.

## Non-goals (v1)

- No AgentCore Registry as source of truth (federated source later — D1).
- No **downgrade** on missing capability (block-only v1 — D5).
- No open plugin-registration framework for primitives yet (concrete five first; generalize on the
  2nd novel primitive — D3).
- No headless/A2A authoring front-end yet — the Agent contract is the shared substrate those lanes
  can target later, but the Designer targets the interactive lane first.

## Open questions

- **Skill vs. Agent reconciliation** — Skills are a second proto-canvas (instructions + reference
  files + bound tools). Do Skills become a *reusable binding bundle* you drop onto an Agent, or fold
  into the Agent entity entirely? Deferred, but the Designer forces the question.
- **Per-invoker capability preview** — should a sharee see *why* an Agent is (or isn't) fully runnable
  for them before invoking (a "you lack: Opus, space X" surface), per D5?
- **Model params governance** — `modelConfig.params` (temperature, max tokens, reasoning effort) are
  free within an allowed model; do any params themselves warrant role-gating?
