# Skills as an Agent Primitive (Skills v2)

**Status:** Direction locked 2026-07-16 (product owner). Supersedes the tool-binding
sections of `admin-skills-rbac-tool-binding.md` and the mode-toggle design in
`skills-mode.md`. The v1 build history and its gotchas remain documented in those
specs; where this document conflicts with them, this document wins.

---

## 1. Summary

A **Skill** becomes a pure knowledge bundle — instructions plus supporting
reference material, packaged per the open [agentskills.io](https://agentskills.io/specification)
standard — with **no tool bindings**. Tools, model, knowledge base, memory
spaces, and skills all bind in exactly one place: the **Agent**, via the Agent
Designer. Skills additionally gain a **user-authored tier**: any user can upload
their own skills and bind them to agents they author.

The runtime replaces our homegrown progressive-disclosure machinery
(`SkillRegistry` dispatcher/executor, MCP tool folding) with the **Strands
`AgentSkills` vended plugin**, which implements the same standard natively and
is present in our pinned `strands-agents==1.47.0`.

### Why

1. **One canvas.** The Agent Designer epic (`agent-designer.md`) established the
   Agent as the single RBAC-governed primitive-binding surface. Skills-with-
   bound-tools was a second, competing proto-canvas — the reconciliation
   question that spec explicitly parked. Answer: skills are an *ingredient* on
   the one canvas, not a canvas.
2. **Deletes a bug class.** Every serious skills bug in v1 came from tool
   folding hiding the real tool name behind `skill_executor`: the OAuth-consent
   hole (PR #477), the approval hole (PR #478), the MCP-Apps `ui_resource`
   non-render, and the still-open early-mount/`ui_tool_input_partial` gap. All
   of that scaffolding exists to serve `bound_tool_ids`. Cut the field and the
   scaffolding — and its failure modes — go with it.
3. **Converges on the open standard.** AgentCore Harness attaches skills in the
   agentskills.io format from a source union (managed catalog / git / S3 /
   filesystem path). Storing our skills as standard bundles makes every skill a
   portable artifact: the same bundle a user uploads to us is directly
   attachable to a managed Harness (`{"s3": {"uri": ...}}`) if/when the headless
   lane adopts it (`managed-harness` spike: adopt-with-boundary).
4. **Framework over custom code.** Strands ships `AgentSkills` + `Skill`
   (programmatic construction, `<available_skills>` system-prompt injection,
   one `skills` activation tool, no execution). Preference confirmed: remove
   custom code in favor of framework-supported code.

### Migration cost: none

`SKILLS_ENABLED=false` in every environment. No production data binds tools to
skills. Existing `bound_tool_ids` values (including the `web_research` seed) are
**silently dropped** — a redesign, not a migration.

---

## 2. What changes

| Area | v1 (built, dark) | v2 (this spec) |
|---|---|---|
| Skill shape | instructions + reference files + `bound_tool_ids` | instructions + reference files (agentskills.io bundle); **no tools** |
| Tool access | skill grants/folds its bound tools | tools come **only** from Agent bindings + RBAC |
| Runtime | `SkillAgent` subclass, `skill_dispatcher`/`skill_executor` meta-tools, MCP folding | `ChatAgent` + Strands `AgentSkills` plugin; `SkillAgent` retired |
| Chat exposure | "skills mode" toggle (agent_type flip), admin chat-mode policy | mode gone; skills selected per-turn like `enabled_tools`, **opt-in by default** |
| Authorship | admin catalog only | admin catalog **+ user-uploaded** (owner-scoped) |
| `allowed-tools` frontmatter | n/a (we had real bindings) | parsed and stored as **advisory metadata only**; never enforced |
| `scripts/` in bundles | out of scope | **accept-and-inert**: stored, listed, labeled non-executable, never run |
| Sharing | n/a | **invoke-through** access via shared Agents (§6) |

### What is deleted (PR-1)

- `bound_tool_ids` on `SkillDefinition` and all DTOs; the admin tool-picker dialog.
- `agents/main_agent/skills/mcp_binding.py` (`FoldedMCPTool`, `resolve_mcp_bindings`,
  `make_folded_tool_provider_lookup`, `make_folded_tool_approval_lookup`).
- `agents/main_agent/integrations/mcp_tool_folding.py` and the `drop_folded_tools`
  calls in `FilteredMCPClient` / `UICapableMCPClient`.
- The `tool_use_provider_lookup` / `tool_use_approval_lookup` fold shims on
  `OAuthConsentHook` / `MCPExternalApprovalHook`, and the `_resolve_ui_tool_name`
  `skill_executor` unwrap in `stream_coordinator`.
- Skills mode: the SPA mode toggle, `preferredAgentMode`, the admin chat-mode
  policy surface, and the `skill`/`chat` forcing in `_resolve_effective_agent_type`.
- (PR-2) the homegrown disclosure stack: `skill_registry.py` catalog/dispatch,
  `skill_tools.py`, `skill_dispatcher`/`skill_executor`, and `SkillAgent` itself.

### What is kept

- `apis/shared/skills/` model/repository/resource_store/access — slimmed, not replaced.
- The `SkillOwnerIndex` GSI4 (`OWNER#`) — provisioned in v1 PR-1 for exactly the
  user-authored tier.
- Admin skills RBAC (`granted_skills`, `/admin/skills/{id}/roles`) and the admin
  skills page (minus the tool picker).
- The skills-mode PR-2 request plumbing, **verbatim**: `enabled_skills` on the
  invocation request, `_apply_enabled_skills_filter` (RBAC ∩ selection),
  `skills_hash` in the agent cache key, `enabled_skills` in the paused-turn
  snapshot. This *is* the "select skills like enabled tools" mechanism.
- Agent Designer skill bindings (REPLACE semantics) — now orthogonal to tools.
- `SKILLS_ENABLED` stays the master gate until v2 is dogfooded.

---

## 3. Decisions

- **D1 — Skill ⊥ tools.** A skill may *mention* tools in its text (prose or
  `allowed-tools` frontmatter); the platform never grants, mounts, or folds a
  tool because a skill names it. The tool universe for a turn is exclusively
  (Agent tool bindings | request `enabled_tools`) ∩ RBAC, unchanged from the
  agent-designer spec.
- **D2 — agentskills.io is the storage and interchange format.** A skill is a
  bundle: `SKILL.md` (YAML frontmatter `name`/`description` + optional
  `allowed-tools`/`metadata`/`license`/`compatibility`; markdown body) plus
  optional `references/`, `scripts/`, `assets/`. Import and export are
  round-trip-faithful.
- **D3 — Strands `AgentSkills` plugin is the runtime** (pending the PR-2 spike,
  §8). `ChatAgent` conditionally adds the plugin when the turn's effective
  skill set is non-empty. No `SkillAgent`, no agent_type `"skill"`.
- **D4 — `allowed-tools` is advisory.** Strands parses it and marks it
  "Experimental: not yet enforced"; we store and display it. Optional Designer
  soft warning when a bound skill declares a tool the agent doesn't bind.
  Never an error, never a grant.
- **D5 — `scripts/` accept-and-inert.** Bundles containing scripts upload
  successfully; scripts are stored and listed with a "not executable on this
  platform" label and are never executed. Execution is a deliberate future
  feature (likely AgentCore Code Interpreter as the sandbox); stored bundles
  are already data-compatible with that future.
- **D6 — Opt-in selection in plain chat.** Unlike tools (opt-out), skills
  default to *disabled*; a user explicitly enables the skills they want. As the
  catalog grows (admin + user-authored), opt-in caps prompt bloat and
  instruction conflicts. Agent-bound skills are always active on that agent —
  the author opted in at design time; invokers do not re-toggle them.
- **D7 — Invoke-through sharing** (§6). Sharing an Agent shares the *use* of
  its bound skills, mirroring the assistant-KB precedent. Chain-sharing is
  blocked by an owner-match clause.
- **D8 — Two authorship tiers, one table.** Admin catalog skills (RBAC
  `granted_skills`) and user-owned skills (GSI4) are the same record type with
  different `owner_id`/governance. No parallel store.

---

## 4. Data model

`SkillDefinition` (DynamoDB `app-roles` table, `PK=SKILL#{id}`, unchanged keys):

```
id, name, description            # frontmatter-equivalent
instructions                     # SKILL.md body
allowed_tools: list[str] = []    # NEW — advisory, parsed from frontmatter
skill_metadata: dict = {}        # NEW — frontmatter passthrough (license, compatibility, metadata)
resources: [SkillResourceRef]    # manifest; ref gains a `kind`: reference|script|asset
owner_id                         # "system" for admin catalog; user id for uploads
status                           # active|disabled
# REMOVED: bound_tool_ids
```

**S3 layout** (existing `skill-resources` bucket) normalizes to the standard
bundle shape so a prefix *is* a valid agentskills.io skill:

```
skills/{skill_id}/SKILL.md            # generated from name/description/allowed_tools + instructions
skills/{skill_id}/references/{file}
skills/{skill_id}/scripts/{file}      # inert (D5)
skills/{skill_id}/assets/{file}
```

DynamoDB remains the source of truth for metadata/instructions; the S3
`SKILL.md` is a write-through projection so the prefix is always attachable
elsewhere (harness lane) and exportable as-is. Content-hash dedupe from v1's
resource store is dropped in favor of plain per-skill keys — bundles are small,
and the standard layout is worth more than dedupe. Per-file cap stays 1 MiB,
per-skill file count cap stays 50 (revisit if real bundles need more; the
harness allows 1 GB).

**Source union (future-shaped, not built now):** the record gains
`source: {kind: "inline"}` today. Later kinds mirror the harness:
`{kind: "s3", uri}` and `{kind: "git", url, path, credential_provider}` (git
auth via the AgentCore Identity token vault we already operate). Deferred until
a concrete need; the field exists so adding kinds is additive.

---

## 5. Runtime

### Effective skill set for a turn

```
own      = skills owned by the invoker (GSI4)
catalog  = RBAC-accessible admin skills (granted_skills, "*" honored)
agent    = skills bound on the invoked Agent that pass §6 access
selected = request.enabled_skills (explicit opt-in ids; None/absent → [])

plain chat turn:   (own ∪ catalog) ∩ selected
agent turn:        agent bindings (REPLACE — selection ignored, as with tools)
```

Note the plain-chat default flips from v1: absent `enabled_skills` now means
**no skills** (D6 opt-in), not "all accessible". `skills_hash` hashes the
effective set exactly as today, so caching and paused-turn resume carry over.

### Agent construction

```python
if effective_skills:
    strands_skills = [
        Skill(name=r.name, description=r.description,
              instructions=r.instructions, allowed_tools=r.allowed_tools or None,
              metadata={"skill_id": r.id, **r.skill_metadata})
        for r in effective_skill_records
    ]
    plugins.append(AgentSkills(skills=strands_skills))
```

The plugin injects `<available_skills>` (~100 tokens/skill) before each
invocation and exposes the `skills` activation tool (L1→L2 disclosure).

### Reference files (L3 disclosure)

The plugin returns resource *listings* for path-backed skills but does not read
files; programmatic `Skill` instances carry no filesystem. We keep one thin
local tool:

```
read_skill_file(skill_name: str, path: str) -> str
```

- Resolves `path` against the skill's `resources` manifest (no traversal —
  manifest lookup, not filesystem).
- Serves bytes from S3 via the existing `SkillResourceStore` (runtime already
  has read-only bucket access from v1 PR-4/6a).
- `scripts/*` requests return the content prefixed with the inert-script notice
  (D5); binary assets return a descriptive note, never raw bytes.
- Enforces the §6 access predicate per call.
- The `Skill.instructions` body is suffixed with a generated "Available
  reference files" section listing manifest paths, so the model knows what it
  can request after activation.

This is the one deliberate piece of custom code that remains; it is the
S3-vs-filesystem adapter, not a parallel disclosure engine.

### What replaces `SkillAgent`

Nothing. `ChatAgent` gains the conditional plugin; agent_type `"skill"` is
removed from the request contract (accepted-but-coerced to `"chat"` during a
deprecation window so stale SPA sessions don't 422).

---

## 6. Sharing & permissions

### The scenario

User A authors a custom skill, binds it to their Agent, shares the Agent with
user B (viewer/editor, issue-#113 model). The binding resolver (agent-designer
D5) re-checks every binding against the *invoker* — B holds no grant on A's
skill, so the naive rule blocks the turn. That would make sharing agents with
custom skills useless.

### Rule: invoke-through access

Precedent: a shared assistant already exposes the owner's KB documents to
invited users *through invocation* — the share is the grant boundary for
content welded to it. A skill is the same class of durable content behind the
same auth (governance = identity claims, not content inspection).

A skill binding on an Agent resolves for an invoker when **any** of:

1. **Catalog grant** — RBAC `granted_skills` reaches the skill; or
2. **Ownership** — the invoker owns the skill; or
3. **Invoke-through** — `skill.owner_id == agent.owner_id` **and** the invoker
   has share-access to the agent.

Clause 3's owner-match blocks **chain-sharing**: a skill shared *to* A cannot
be laundered to a wider audience by binding it to A's agent and sharing the
agent — invoke-through only extends the agent owner's *own* skills.

Failing all three → the existing D5 block-with-message behavior.

`read_skill_file` applies the same predicate on every call (the tool is the
only read path for reference bytes at runtime).

**Design-time rule (unchanged shape):** bindable skills = own ∪ catalog-granted.
You cannot bind a skill that was merely shared to you (until explicit
skill-level shares exist, phase 2).

**Disclosure:** sharing an agent shares its skill *content* — invokers can read
reference files through disclosure and instructions shape visible behavior.
Identical to the KB posture; the share dialog copy must say it: *"People with
access can use this agent's skills and knowledge."*

---

## 7. Surfaces

- **Admin skills page** — kept as-is minus the tool-picker dialog; gains the
  standard-bundle upload (below) and an `allowed-tools` display chip.
- **My Skills (new user page)** — list/create/edit/delete own skills; upload a
  `SKILL.md` + individual files (existing `parseSkillMarkdown` import-prefill;
  zip support still deferred — no new dep). Reference/script/asset files upload
  into the standard layout; scripts get the inert label.
- **Chat input** — skills join the model-settings panel as an opt-in picker
  (per-skill user prefs from v1 persist the selection; default unchecked per
  D6). No mode toggle.
- **Agent Designer** — the existing skill multi-select now sources its palette
  from `GET /agents/bindable?kind=skill` = catalog-granted ∪ own. Optional D4
  soft warning row. Chat-input lock behavior for bound skills (PR #603/#606
  machinery) carries over unchanged.

---

## 8. Plan

- **PR-1 — The great deletion.** Remove `bound_tool_ids` end-to-end, the fold
  machinery, the hook shims, the stream-coordinator unwrap, skills mode
  (backend policy + SPA toggle). Flag stays off; suite green. Largest-negative-
  diff PR of the epic.
- **PR-2 — Spike + runtime swap.** *Spike gate:* verify `AgentSkills` prompt
  injection composes with our prompt assembly, agent cache (`skills_hash`),
  and paused-turn resume; verify `agent.state` persistence of active skills
  survives our session manager. Go → wire plugin into `ChatAgent`, add
  `read_skill_file`, retire `SkillAgent`/`skill_registry` disclosure, normalize
  S3 layout + SKILL.md projection. No-go → fall back to the (much smaller,
  fold-free) homegrown dispatcher and record why.
- **PR-3 — User-authored tier.** Owner-scoped `/skills` CRUD + upload routes
  (session auth), GSI4 list-my-skills, My Skills page.
- **PR-4 — Selection surfaces.** Chat opt-in picker; Designer palette union;
  invoke-through predicate in the binding resolver + `read_skill_file`;
  share-dialog copy.
- **PR-5 — Flip.** `SKILLS_ENABLED=true` on dev-ai; dogfood with a real
  Anthropic-format bundle (e.g. their `docx` skill) uploaded as a user skill
  and bound to an Agent.
- **Later:** `git`/`s3` source kinds; explicit skill-level shares; script
  execution via a sandbox; harness-lane attachment of the same bundles.

---

## 9. Open items

- **Spike go/no-go (PR-2)** is the only open technical risk; everything else is
  deletion or reuse of proven v1 machinery.
- **`enabled_skills` default flip** (absent → none, per D6) — confirm no client
  besides the SPA sends skill turns before the flip lands (API-key surface
  sends none today; it inherits opt-in semantics naturally).
- **Deprecation window** for `agent_type="skill"` in stored session preferences
  and paused snapshots: coerce to `"chat"` on read; remove after one release.
