# Memory Spaces — user-owned, shareable markdown "second brains" for agents

**Status:** Draft / proposal (reframed 2026-07-07 from "per-user markdown memory" to the **Memory Space** primitive)
**Author:** (drafted with Claude)
**Date:** 2026-06-27 · reframed 2026-07-07
**Targets branch:** `develop`
**Related:** skills reference-file / progressive-disclosure pattern (`agents/main_agent/skills/`, `apis/shared/skills/resource_store.py`); AgentCore Memory write-only limitation (`agents/main_agent/session/turn_based_session_manager.py`); context attribution (`apis/shared/costs/`); assistant sharing / collaborative editing (issue #113, `resolve_assistant_permission`); agentic-platform primitives epic F5 (`docs/specs/agentic-platform-primitives.md`); scheduled runs (`apis/app_api/schedules/`)

## Summary

Give every user one or more **Memory Spaces** — named, human-readable **markdown wikis** that
agents read at the start of a conversation and maintain over time. A Memory Space is a
per-owner (optionally **shared**) "second brain": a tiny always-loaded **index** (`MEMORY.md`)
of one-line pointers, plus a set of **typed markdown entries** fetched on demand. The shape is
the one Karpathy popularized and the one this very repo's coding agent uses internally.

The reframe from the original draft: memory is **not** "the user's one flat pile of facts." It
is a **first-class, named, bindable primitive** — a Memory Space — that a user can have several
of, that agents **bind** to declaratively, that ships with **templates** (Chief of Staff,
Research Notebook, blank wiki), and that can be **shared** with other users. **Oliver is not a
feature; Oliver is a "Chief of Staff" template + an agent bound to a space.**

The architectural move is that we already ship this mechanism — it's the **skills reference-file
/ progressive-disclosure** path ([`skill_registry.read_resource`](../../backend/src/agents/main_agent/skills/skill_registry.py),
[`SkillResourceStore`](../../backend/src/apis/shared/skills/resource_store.py)): S3
content-addressed storage, a lightweight DynamoDB manifest, **server-side read
mid-conversation**, and a Level-1 catalog injected into the system prompt with Level-2+ files
fetched via a tool. A Memory Space is that mechanism **re-scoped from per-skill to per-space**,
plus a **write/consolidation** path, a **binding** model, and a **sharing** model.

This is the right design (vs. leaning on AgentCore Memory) because **AgentCore Memory is
write-only in cloud** — the SDK restore branch never fires, so Memory cannot be the read-time
source of truth today (see
[`project_session_restore_writeonly_memory`](../../backend/src/agents/main_agent/session/turn_based_session_manager.py)
analysis). Managed AgentCore memory (on-by-default on a harness) covers the **opaque
conversational-continuity** slice; a Memory Space covers the **inspectable, editable,
entity-linked knowledge** slice. They are complementary, not competitors.

---

## The abstraction (the core of this spec)

Oliver — a chief-of-staff agent with full institutional memory — decomposes into **three
separable layers**. The middle one, generalized, is the primitive.

```
┌─ AGENT (persona + behavior) ──────────────────────────┐
│  "You are Oliver… wake-up protocol… how you think"     │  ← instructions (assistant / harness config)
│                                                        │     + bound tools (calendar, drive, …)
└───────────────────┬───────────────────────────────────┘
                    │  binds to (declarative)
                    ▼
┌─ MEMORY SPACE (the primitive) ────────────────────────┐
│  MEMORY.md          ← always-on index / orientation    │
│  entries/ people/ projects/   (entity, mutable)        │  ← markdown + frontmatter
│           daily/ briefs/       (episodic, append-only)  │     + [[wikilinks]] = the graph edges
│  manifest (DynamoDB)          ← indexed fields → query  │
│  members            ← owner + shared grants (viewer/editor)
└───────────────────┬───────────────────────────────────┘
                    │  rendered by
                    ▼
     SPA "Memory" panel  ← view / edit / export / forget-me / share
```

### Two kinds of "connectedness" — and they live in different places

A natural instinct is to put "how everything connects" into the agent's instructions plus a
`MEMORY.md`. That is **half right**, and the correction is the whole point of making this a
primitive rather than a prompt convention:

| Kind | What it is | Where it lives | Enforced by |
|---|---|---|---|
| **Structural** | *which* space(s) an agent reads/writes, access mode, which entries always-load, entry schema, who may read/write | **Declarative config** on the agent record + the space record | **Platform** (RBAC, deterministic index hydration, edit UI, sharing) |
| **Semantic** | what the wiki *means*, how entries relate, how to reason across them | **`MEMORY.md` index + `[[wikilinks]]` in entries + agent instructions** | The **LLM** reads it |

Putting the *structural* wiring in freeform prose ("remember to read your people files") keeps it
a brittle, unenforceable convention with no access control and no edit surface. Making the
binding **declarative** turns it into a primitive: the platform can enforce who reads/writes,
hydrate the index identically every wake-up, render the "what I remember" panel, and share the
space. **`MEMORY.md` is the content map; instructions are the behavior; the binding is config.**

### Memory Space is the fourth bindable primitive

The platform already binds **tools**, **skills**, and **KBs** to assistants. A **Memory Space**
binds the same way and is governed the same way:

- **Registry / RBAC (F6):** spaces are catalogable and access-controlled exactly like tools and
  skills — read vs. write grants, sharing, audit. (Sharing is F5 × F6.)
- **Scheduler (Phase B, shipped):** binding a space to a *scheduled* run is what turns a passive
  notebook into Oliver — a nightly run scans the manifest for stale commitments and surfaces
  them unprompted. "Presence, not a tool" = Memory Space (F5) + proactive trigger (already built).

---

## Concepts (glossary)

- **Memory Space** — a named, first-class container (`SPACE#{space_id}`) holding an index
  (`MEMORY.md`), a set of typed entries, a DynamoDB manifest, and a member list. Owned by one
  user; optionally shared with others (viewer/editor). A user may own/belong to many.
- **Entry** — one markdown file with frontmatter. Three built-in **entry types**:
  - `entity` — a mutable record keyed by subject (a person, a project). Updated in place.
  - `episodic` — an append-only, dated record (a daily log, a brief). Latest N ride the index.
  - `fact` — a flat distilled fact (the original spec's unit). The catch-all.
- **Space Template** — a preset that seeds a new space: which entry types, always-load rules, and
  a starter `MEMORY.md`. Ships: **Chief of Staff**, **Research Notebook**, **Blank Wiki**.
- **Binding** — declarative config on an agent/assistant that lists the space(s) it reads/writes,
  the access mode, and the always-load manifest (which entries hydrate at wake-up).
- **Member / grant** — a `(user, role)` pair on a space. Roles: `owner`, `editor`, `viewer`
  (mirrors assistant sharing, issue #113).

---

### Prior art

- **Karpathy's LLM Wiki / second brain** — agent-maintained markdown wiki: immutable raw
  sources, an AI-generated/-maintained wiki layer, and a schema file governing read/write/
  reconcile. Core claim: LLMs don't get bored, so the wiki-maintenance burden that kills human
  wikis disappears. ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f))
- **MRAgent — "Memory is Reconstructed, Not Retrieved"** ([arxiv 2606.06036](https://arxiv.org/abs/2606.06036),
  [repo](https://github.com/Ji-shuo/MRAgent)) — interleaves reasoning with memory access rather
  than a static retrieve-then-reason step. Transferable lesson: **don't pre-stuff context; let
  the agent fetch on demand inside its loop.**
- **This repo's own coding-agent memory** — `MEMORY.md` index + one-fact-per-file markdown with
  frontmatter (`name`/`description`/`type`), `[[wikilinks]]`, a consolidation pass. The cleanest
  reference implementation of exactly what we'd build.
- **The Oliver skill** (`oliver:oliver`, `~/Documents/memory/`) — a *live, working* instance of
  this exact pattern: `MEMORY.md` index + `people/` and `projects/` entity files + `daily/` and
  `briefs/` episodic files, with a wake-up protocol that always-loads the index + latest daily +
  latest brief, then pulls entries on demand. Oliver is the worked example this spec generalizes
  (see [Oliver as a template](#oliver-as-a-worked-example)).

## Goals

- A **Memory Space** primitive: named, durable, **human-readable** markdown a user can own several
  of, that agents read at conversation start and update over time.
- **Templated** — new spaces seed from Chief of Staff / Research Notebook / Blank so ergonomics
  come for free without hardcoding any one use case.
- **Bindable** — an agent/assistant declares which space(s) it uses and how, as config.
- **Token-bounded**: steady-state cost is the index only; entry bodies are lazy.
- **User-visible & user-editable** — "here's what I remember," with edit / delete / export. This
  is a product feature (control over what's remembered), and it's how agent-written memory stays
  correctable.
- **User-owned & portable** — a user can **download the entire space as a `.zip`** of its raw
  markdown (index + all entries, directory structure preserved) at any time. Full ownership, zero
  lock-in: the export is the complete, human-readable, re-importable corpus — the same property
  `agentcore export harness` gives on the run side, and something vector RAG can't offer. See
  [Export / download](#9-export--download-full-ownership).
- **Shareable** — a space can be shared with other users (viewer/editor), enabling team wikis and
  shared institutional memory (phased; see [Sharing](#sharing--access-control)).
- Reuse the **skills reference-file mechanism** (S3 store + DynamoDB manifest + on-demand read
  tool) re-scoped to `SPACE#{space_id}`, with minimal new infrastructure.
- Ship **dark behind a flag** (`MEMORY_SPACES_ENABLED`, default off), per-environment enable,
  mirroring `SKILLS_ENABLED`.

## Non-goals (v1)

- A graph/vector memory (MRAgent's full Cue–Tag–Content graph). v1 is markdown + an index +
  a few indexed manifest fields; the *reconstruction* lesson is adopted (on-demand fetch), the
  graph store is not. Wikilinks are the edges.
- Importing raw source documents into a wiki (Karpathy's "raw sources" layer). A space holds
  distilled entries, not a document corpus — that overlaps the existing RAG/assistant KB.
- Cross-space linking. A `[[wikilink]]` resolves **within** its space in v1.
- **Full org-shared memory in the first release** — the storage is *keyed for sharing from day
  one* (see below), but the grant/collaboration surface is a distinct later phase (A4). Sequencing,
  not a governance gate — access control is identity-based and inherits the platform posture.
- Replacing AgentCore Memory or the compaction/summary path — this is additive.

---

## Background: the pattern we're extending

Skills already do per-skill progressive disclosure end-to-end. Much of this spec is re-pointing
it at spaces and adding writes + binding + sharing.

```
SkillResourceRef (DynamoDB manifest row)        ← lightweight pointer, no bytes
  filename, content_hash, size, content_type, s3_key

SkillResourceStore (S3, content-addressed)      ← put() dedupes on hash, get() fetches
  skills/{skill_id}/{content_hash}

Runtime:
  get_catalog()        → Level-1 listing injected into system prompt
  get_resource_names() → filenames offered to the model (no bytes)
  read_resource(name)  → server-side S3 fetch mid-conversation, returns text
```

Concrete anchors:
- Manifest model: [`SkillResourceRef`](../../backend/src/apis/shared/skills/models.py)
- S3 store: [`SkillResourceStore`](../../backend/src/apis/shared/skills/resource_store.py)
- Runtime disclosure: [`skill_registry.py`](../../backend/src/agents/main_agent/skills/skill_registry.py) (`get_catalog`, `get_resource_names`, `read_resource`)
- Bucket construct: [`skill-resources-construct.ts`](../../infrastructure/lib/constructs/skills/skill-resources-construct.ts)
- System-prompt assembly: [`system_prompt_builder.py`](../../backend/src/agents/main_agent/core/system_prompt_builder.py), per-turn resolution in [`system_prompt_resolver.py`](../../backend/src/apis/inference_api/chat/system_prompt_resolver.py)
- Sharing chokepoint to mirror: `resolve_assistant_permission` (issue #113, assistant viewer/editor)
- Token accounting we'll reuse: `contextBreakdown` partitions in [`costs/models.py`](../../backend/src/apis/shared/costs/models.py)

**The asymmetries that matter:** skill resources are **read-only, admin-authored, per-skill**; a
Memory Space is **read-write, user/agent-authored, per-space, and shareable**. The store,
manifest, and read tool transfer directly. The new work is (1) the **write + consolidation**
path, (2) the **binding** model, and (3) the **sharing / access-control** model.

---

## Design

### 1. Storage layout — keyed by **space**, not by user

Sharing forces the identity decision up front: a shared space **cannot** live under any one
user's partition. The space is its own entity; ownership and membership are records *about* it.

```
S3 (content-addressed, one bucket):
  spaces/{space_id}/MEMORY.md                   # the index — small, always-loaded
  spaces/{space_id}/entries/{type}/{slug}.md    # one entry per file, fetched on demand
      entries/entity/jane-doe.md
      entries/project/agentcore-v2.md
      entries/episodic/2026-07-07-daily.md
```

```
DynamoDB (dedicated `memory-spaces` table — the project uses per-domain tables,
not one global table; a dedicated table avoids GSI-number collisions and lets the
MemorySpacesConstruct own both the bucket and the table):
  PK=SPACE#{space_id}  SK=META                     # space name, template, owner_id, created, index pointer
  PK=SPACE#{space_id}  SK=INDEX                     # manifest: [{slug,type,description,content_hash,size,updated,updated_by,indexed:{...}}]
  PK=SPACE#{space_id}  SK=MEMBER#{email}            # role: viewer|editor (owner lives on the META row)
```

Owned vs. shared-in are listed via **two GSIs** unioned in code — mirroring
assistant sharing (owner index + share-by-email index), rather than one combined
index (the proven pattern; a single `USER#`-keyed index can't cover both an
`owner_id` and an invited-by-`email` grant):

```
  OwnerIndex   GSI1PK=OWNER#{owner_id}   GSI1SK=SPACE#{space_id}   # list owned spaces
  MemberIndex  GSI2PK=MEMBER#{email}     GSI2SK=SPACE#{space_id}   # list shared-in spaces
```

Each entry file carries frontmatter (mirrors the coding-agent memory shape; adds type + author):

```markdown
---
name: jane-doe
type: entity            # entity | episodic | fact
description: VP Research; owns the NSF AI grant relationship
subject: Jane Doe       # entity key (entity type)
status: active
commitments:            # indexed → manifest, so "who owes what" is a query not a scan
  - { owed_by: phil, desc: "send grant draft", due: 2026-07-12, open: true }
updated: 2026-07-07
updated_by: 18419330-…  # write attribution (essential for shared spaces + forget-me)
---

Jane cares about defensible governance… Link related entries with [[agentcore-v2]].
```

**No bodies in DynamoDB** (400 KB item-limit rule, same reason skills went to S3). The manifest
row is the fast-path pointer + cache-key input + the **indexed-field query surface** (a small
allowlist of frontmatter fields — e.g. `type`, `status`, `commitments.due`, `updated` — copied
into `indexed` so aggregate/temporal queries don't load every body).

### 2. New shared service: `MemorySpaceStore` + `MemorySpaceService`

New package `apis/shared/memory/` (a shared concern: both app-api and inference-api consume it,
so it lives in `apis.shared` per the import-boundary rule).

```python
# apis/shared/memory/store.py  — space-keyed, mirrors SkillResourceStore
class MemorySpaceStore:
    def read_index(self, space_id: str) -> str: ...
    def list_entries(self, space_id: str, *, type: str | None = None,
                     where: dict | None = None) -> list[MemoryEntryRef]: ...   # manifest query
    def read_entry(self, space_id: str, slug: str) -> str: ...
    def write_entry(self, space_id: str, slug: str, body: str, *, author: str) -> MemoryEntryRef: ...
    def update_index(self, space_id: str, body: str) -> None: ...
    def delete_entry(self, space_id: str, slug: str) -> None: ...

# apis/shared/memory/service.py — space lifecycle + access control
class MemorySpaceService:
    def create_space(self, owner: str, name: str, template: str) -> MemorySpace: ...
    def list_spaces_for_user(self, user_id: str) -> list[MemorySpace]: ...      # owned + shared-in (GSI)
    def resolve_permission(self, space_id: str, user_id: str) -> Role | None: ...# THE chokepoint
    def share(self, space_id: str, actor: str, grantee: str, role: Role) -> None: ...
    def revoke(self, space_id: str, actor: str, grantee: str) -> None: ...
```

Mirror `SkillResourceStore`: lazy boto3 init, content-addressed objects, raise loudly on miss,
best-effort delete. Env var `S3_MEMORY_SPACES_BUCKET_NAME`. **Every read/write path routes
through `resolve_permission`** — the single chokepoint, exactly like `resolve_assistant_permission`.

### 3. Binding — how an agent knows which space(s) it uses

Declarative, on the assistant/agent record (not in prose):

```jsonc
// assistant / agent config
"memorySpaces": [
  { "spaceId": "spc_oliver", "access": "readwrite",
    "alwaysLoad": ["MEMORY.md", "latest:episodic/daily", "latest:episodic/brief"] }
]
```

- `access`: `read` | `readwrite`. Enforced against the invoking user's grant on the space (a
  `readwrite` binding still requires the *user* to hold `editor`+; see [Sharing](#sharing--access-control)).
- `alwaysLoad`: which entries hydrate at wake-up. `latest:episodic/daily` is the Oliver rule
  ("most recent daily + brief"), resolved from the manifest at bind time.
- Default binding = the user's **personal** space (auto-created on first use), read-write, index-only
  always-load. An assistant can bind additional/other spaces.

### 4. Read path — index in prompt, entries on demand

**Index injection (Level 1).** At conversation start, inject each bound space's `MEMORY.md`
(plus `alwaysLoad` entries) as a bounded block. Only the index is pre-loaded; bodies are
reconstructed on demand (MRAgent discipline).

> **Prompt-cache constraint (decide here).** Per-owner content in the system prefix gives each
> user a distinct cache key and interacts badly with shared-prefix caching
> ([`system_prompt_resolver.py`](../../backend/src/apis/inference_api/chat/system_prompt_resolver.py)).
> - **(A) Dedicated cache block.** Append the memory index as its own `cache_control` breakpoint
>   after the shared platform prefix. Platform prefix stays globally cached; the index caches
>   *within* the session. **Preferred.** *Bonus for shared spaces:* a shared space's index is
>   byte-identical across members, so its cache block can be reused across all members of the
>   space — a caching **win** unique to shared spaces.
> - **(B) Tool-loaded on turn 1.** Agent calls `memory_index(space)` on turn 1. Zero prefix
>   disruption, one extra round-trip.
>
> Recommend **(A)**; spike both and measure with `contextBreakdown`.

**Entry fetch (Level 2).** `memory_read(space, slug)` fetches one entry body server-side from S3
— a direct analog of `read_resource`. `memory_query(space, where)` runs a manifest query (e.g.
`{type: entity, "commitments.open": true}`) returning refs, not bodies — this is how "who owes
what" and staleness scans avoid a full-corpus load.

### 5. Write path

- **(W1) Synchronous agentic write** — `memory_write(space, slug, body)` /
  `memory_update(space, slug, patch)` tools the model calls mid-turn, stamping `updated_by` with
  the invoking user, plus an index-update step. Transparent, matches the coding-agent model.
  **v1 default.**
- **(W2) Async post-turn reflection** — a background job reads the completed turn and proposes
  edits, hung off [`turn_based_session_manager.update_after_turn`](../../backend/src/agents/main_agent/session/turn_based_session_manager.py)
  (the seam compaction uses). Reliable, no in-loop discipline needed, adds an LLM call/turn.
  **Phase 2.**

**Consolidation.** A periodic pass (Karpathy's insight — LLMs don't get bored) that merges
duplicate entries, fixes stale ones, prunes the index, enforces the index cap. Run as a
**scheduled job per space** (the shipped scheduler) or lazily on threshold. Mirrors the repo's
`consolidate-memory` skill.

### 6. Sharing & access control

The reason the storage is space-keyed. A space is shared by granting other users a role on it,
mirroring assistant sharing (issue #113) so the model, API shape, and SPA dialog are familiar.

- **Roles:** `owner` (full control + manage members + delete space), `editor` (read + write
  entries + index), `viewer` (read only). `resolve_permission` is the chokepoint every route and
  every agent tool passes through — no write path bypasses it.
- **Agent writes in a shared space carry the invoking user's identity.** The run-as-user model
  (headless-grant / act-as-user, already built for scheduled runs) means an agent's
  `memory_write` executes *as* the invoking user; the platform checks that user holds `editor`+.
  A scheduled Oliver run writing to a shared team space writes as its owner, audited by
  `updated_by`.
- **Write attribution + audit.** Every entry and manifest row carries `updated_by`; shared-space
  edits are attributable. This is both a collaboration affordance ("Jane last edited this") and
  the audit trail.
- **Concurrency.** Multi-writer becomes real in shared spaces. Content-addressed entry writes +
  **optimistic concurrency on the manifest row** (conditional update on a version attribute);
  last-write-wins on individual entries with a visible "edited by X at T" so overwrites are
  legible, not silent.
- **Membership API** (mirrors #113): `POST /spaces/{id}/shares`, `PATCH /spaces/{id}/shares`
  (upgrade viewer↔editor), `DELETE /spaces/{id}/shares/{user}`. `sharedWith` is a
  `ShareEntry[]` (role-carrying), not `string[]`.

### 7. Infrastructure

New `MemorySpacesConstruct` (clone of [`skill-resources-construct.ts`](../../infrastructure/lib/constructs/skills/skill-resources-construct.ts)):
S3 bucket (encryption **matching the platform's sessions/artifacts standard** — don't
special-case memory), block public access, enforce SSL, **no auto-expiry** (memory is durable;
deletion is explicit/user-driven — see [Data governance](#data-governance-proportionate--not-a-special-category) on the dedup-aware purge). Thread the bucket to compute roles via
`PlatformComputeRefs` (typed ref, not SSM), per the construct rules. Read+write grant to
inference-api runtime role and app-api role. The **same construct also owns a dedicated
`memory-spaces` DynamoDB table** (PK/SK, PAY_PER_REQUEST, PITR) with the `OwnerIndex` +
`MemberIndex` GSIs — a per-domain table (the project's actual pattern) rather than a GSI grafted
onto `sessions-metadata`, threaded to both roles as `DYNAMODB_MEMORY_SPACES_TABLE_NAME`.

### 8. User-facing surface (app-api + SPA)

Because a space is human-readable markdown, expose it. `app-api` routes under `/memory/spaces/`
(user-facing, `Depends(get_current_user_from_session)` — **not** Bearer, per the auth rule; every
handler calls `resolve_permission`):

- `GET /memory/spaces` — list the user's spaces (owned + shared-in)
- `POST /memory/spaces` — create from a template
- `GET /memory/spaces/{id}` — index + entry manifest
- `GET /memory/spaces/{id}/entries/{slug}` — read one entry
- `PUT` / `DELETE /memory/spaces/{id}/entries/{slug}` — user edits/deletes an entry (editor+)
- `POST|PATCH|DELETE /memory/spaces/{id}/shares[...]` — manage members (owner)
- `GET /memory/spaces/{id}/export` — **download the whole space as a `.zip`** (see §9)
- `DELETE /memory/spaces/{id}` — delete (owner) / for a shared-in space, **leave** (drop own grant)

SPA: a **Memory** section — a list of spaces, and per space a "what I remember" panel (view,
edit, delete, **download `.zip`**), a **share dialog** (reuse the assistant-share component +
`redesign-tokens`), and a **create-from-template** flow. This is the differentiator vector RAG
can't offer — and the user's control surface over what's remembered.

### 9. Export / download (full ownership)

A user can download the **entire space** as a single `.zip` of its raw markdown at any time —
the concrete expression of "you own this data." Because a space *is* human-readable markdown,
the export is loss-free: it is the complete corpus, not a rendering of it.

- **Contents.** The zip mirrors the S3 layout so it is self-contained and re-importable:
  ```
  {space-name}/
    MEMORY.md                        # the index, verbatim
    entries/entity/*.md              # every entry, with frontmatter intact
    entries/episodic/*.md
    entries/fact/*.md
    metadata.json                    # space name, template, created, members (roles), export timestamp
  ```
  Entry frontmatter (`type`, `updated`, `updated_by`, `[[wikilinks]]`, indexed fields) **is** the
  source of truth; the DynamoDB manifest is a derived cache and is *not* needed in the export —
  it can be rebuilt from the files on import. `metadata.json` carries the small amount of
  space-level state the files don't.
- **Access.** Any member with read (viewer+) may export the content they can already read; the
  owner exports the full space. Routes through `resolve_permission` like every other path.
- **Mechanics.** Built server-side in `app-api`: read the manifest → `get` each object from the
  content-addressed store → stream a zip response (`Content-Disposition: attachment`). Stream
  rather than buffer so large spaces don't pin memory; the entry count is bounded by the
  consolidation cap so this stays modest.
- **Round-trip (future, non-goal v1).** The export format is deliberately import-friendly — a
  later `POST /memory/spaces/import` could reconstruct a space (and rebuild the manifest) from
  this exact zip, giving true portability between environments/accounts. Symmetry now, import
  later.

---

## Oliver as a worked example

Oliver is **not** special-cased. It is:

1. A **space** created from the **Chief of Staff** template, which seeds:
   - entry types: `entity` (people, projects), `episodic` (daily, briefs)
   - `alwaysLoad`: `[MEMORY.md, latest:episodic/daily, latest:episodic/brief]`
   - a starter `MEMORY.md` with sections for strategic priorities, key people, active projects,
     open commitments.
2. An **assistant** ("Oliver") whose **instructions** carry the persona + wake-up protocol +
   how-to-think, **bound** to that space `readwrite`.
3. Optional: the space **shared** `viewer` with a chief-of-staff's delegate, or a scheduled run
   bound to it that scans `memory_query(space, {"commitments.open": true, "commitments.due": "<7d"})`
   nightly and surfaces stale commitments — the proactive behavior, built from primitives.

Every capability Oliver has ("who owes what," "prep me for my 2pm," "someone's owed Phil for two
weeks — flag it") maps to: index always-load + `memory_query` over indexed commitment fields +
entity entries + the scheduler. **A "Research Notebook" space with a different template and a
different bound assistant reuses all of it.**

---

## Token efficiency analysis

- **Steady-state overhead = index only.** ~1 line (~15–25 tokens) per entry. 50 entries ≈
  **~1k tok/turn**; 200 ≈ **~4k tok/turn**. Bounded/tunable via the consolidation cap. Entry
  bodies cost **zero** unless fetched.
- **Lazy bodies (MRAgent discipline).** You pay a body's tokens only on turns that fetch it.
- **Indexed queries beat scans.** "Who owes what" is a manifest query over `commitments.open`,
  not a load of every person file — the difference between O(1 row set) and O(all bodies).
- **Shared-space cache win.** A shared index caches once across all members (strategy A block),
  unlike per-user memory.
- **Versus alternatives:** transcript replay is unbounded (what compaction fights); vector RAG
  pays embedding + opaque chunk injection and isn't user-inspectable; AgentCore summaries are
  unusable for read in cloud.
- **Measurement.** Add a `memory` partition to `contextBreakdown` (same method skills PR-7 used to
  confirm `toolTokens` dropped).

**Net:** capped index + lazy fetch + indexed queries ⇒ **~1–4k tok/turn steady-state**, far
cheaper and more controllable than replay or RAG. The one cost risk is prompt-cache disruption,
mitigated by strategy (A).

---

## Data governance (proportionate — not a special category)

**A Memory Space is the same data class as the content the platform already stores** — session
transcripts, artifacts, assistant KBs, uploaded docs — behind the same **Entra-backed JWT + RBAC**.
It is durable, sometimes model-derived, and (when shared) cross-user, but every one of those
properties is already true of stored conversations. Memory introduces **no new legal boundary**
that transcripts don't already cross, so it **inherits the platform's existing data-governance
posture** rather than needing a bespoke one. Access control is the whole of the exposure story,
and identity claims already own it: `resolve_permission` gates every read/write, exactly like the
rest of the app.

Governance is enforced by **identity, not by content inspection.** There is no agentic-write
redaction / content-scrubbing pass — a shared space is a *deliberate grant*, and the sharing user
is responsible for its contents, exactly as when they share an assistant or a document today. Do
not add friction the rest of the platform doesn't have.

What *is* worth doing — three cheap defaults, none of which gate the build:

- **Encryption at rest = inherit the platform standard.** Use whatever the sessions/artifacts
  buckets use (CMK or AES256); don't special-case memory.
- **Deletion must actually purge.** The one genuine operational wrinkle: memory is durable by
  design (no TTL) and the store is content-addressed/deduped, so a delete/offboarding path must
  really remove the bytes (mind the dedup — a shared object may back multiple entries). This is a
  "make delete work" engineering detail, not a compliance project.
- **Agent-write visibility = the feature you already want.** Because the model writes memory, the
  user can see and correct what's remembered via the "what I remember" panel (§8). The governance
  win falls out of the feature for free — zero added friction.

Plus one UX line: the **share dialog states the scope** ("sharing includes current and future
entries, including ones the agent writes later"), since entries accrue after the grant. That's a
sentence, not a gate.

---

## Phasing (proposed PRs) — two workstreams

**The boundary (decided 2026-07-07):** this epic delivers the **Memory Space primitive and its
user-facing "own your data" surface** — the corpus, its store/service API, and the ways a *person*
manages it (CRUD, export, sharing, the SPA panel). It does **not** deliver the ways an *agent
consumes* it: the `memory_*` tools, the declarative binding, and the system-prompt index
injection are **agent-consumption**, and they live in the **Agent / Harness workstream** (below).

Rationale: a Memory Space is the **4th bindable primitive** (alongside tools/skills/KBs). Welding
its consumption tools/prompt-wiring into the memory epic would bind it to one agent surface
(today's `inference-api`). Keeping consumption in the Agent/Harness layer lets *any* surface —
interactive `inference-api`, our headless harness entrypoint, or an adopted managed Harness — bind
and consume the same primitive. The primitive exposes only `MemorySpaceService`; the agent layer
calls it.

### Workstream A — Memory Spaces epic (primitive + user surface)

- **A1 — Data layer.** ✅ (PR #582) `apis/shared/memory/` (`MemorySpaceStore` + `MemorySpaceService`
  + `MemoryEntryRef`/`MemorySpace`/`MemoryIndex`/`SpaceMember` models + space-keyed repo +
  `resolve_permission` + Blank/Chief-of-Staff/Research-Notebook templates). `MemorySpacesConstruct`
  (CDK) = the S3 bucket + a dedicated `memory-spaces` table with `OwnerIndex`/`MemberIndex` GSIs,
  wired to both compute roles (readwrite). moto tests. No runtime wiring. Flag
  `MEMORY_SPACES_ENABLED`, default off.
- **A2 — User surface (app-api CRUD).** `app-api` `/memory/spaces/` routes over `MemorySpaceService`
  (`Depends(get_current_user_from_session)`, router mounted behind the flag): list / create-from-
  template / get / delete-or-leave, entry read/list/upsert/delete, index read/update. Error
  translation (`NotFound→404`, `Permission→403`). Route tests.
- **A3 — Export / download (§9).** ✅ `GET /memory/spaces/{id}/export` → streamed `.zip` of the raw
  markdown (index + `entries/<type>/*.md` + `metadata.json`). The "own your data" leg.
  `MemorySpaceService.export_space` gathers the corpus (viewer+, members only for editor+); the
  route builds the zip in a `SpooledTemporaryFile` (spills to disk so a large space never pins
  memory) and streams it. Archive path components are sanitized against zip-slip. Route tests.
- **A4 — Sharing.** ✅ Membership API (`GET|POST|PATCH|DELETE .../shares`) over the `MEMBER#` rows
  (owner grants/updates/revokes viewer|editor; editor+ lists) + shared concurrency: the manifest
  `INDEX` row now takes a **conditional write** on `version` (`put_index(expected_version=…)` →
  `OptimisticLockError`), and `write_entry`/`delete_entry` run a bounded read-modify-retry loop
  (`_mutate_index`) that converges on transient races and surfaces `MemorySpaceConcurrencyError`
  (→ 409) only on a sustained one. Access control is identity-based; no content gate.
- **A5 — SPA Memory panel.** ✅ The Memory section under `frontend/ai.client/src/app/memory-spaces/`:
  a list page (owned + shared-in cards with role/template badges), a detail page (view/edit the
  `MEMORY.md` index + entry list with view/edit/create/delete via a dialog), create-from-template
  dialog, download `.zip` (blob → anchor), and a share dialog (add-by-email + per-row role + delta
  save over the A4 endpoints). Signal facade + API service mirror the assistants/schedules pattern;
  nav entry gated on a live `accessible$` probe (404 = kill switch off → hidden), redesign-tokens
  throughout. Viewer = read-only. Facade spec green; dev build + tsc clean.
- **A6 — Consolidation.** A maintenance job (scheduled per space, or on index-cap threshold) that
  merges duplicate entries, fixes stale ones, and prunes the index. Uses `MemorySpaceService`; runs
  on the shipped scheduler. Primitive-side corpus health, not a per-turn concern.

### Workstream B — Agent / Harness consumption (binds the primitive)

These are **not** part of the memory epic; they are how an agent surface reads/writes a bound space.
They plug into `inference-api` today and ride whatever run surface a given lane uses.

- **B1 — Binding.** `memorySpaces` declarative config on the assistant/agent record (`spaceId`,
  `access`, `alwaysLoad`); resolution against the invoking user's grant; default personal-space
  auto-create. This is the seam that connects an agent to a space.
- **B2 — Read path.** `memory_read` / `memory_query` tools (over `MemorySpaceService`) + system-
  prompt index injection (strategy A, the prompt-cache decision) + a `memory` partition in
  `contextBreakdown`. Wake-up hydration (`alwaysLoad`, `latest:` resolution).
- **B3 — Write path (agentic, W1).** `memory_write` / `memory_update` tools + `updated_by`
  attribution, through the tool RBAC/registry.
- **B4 — Reflection writes (W2), optional.** Post-turn reflection on the `update_after_turn` seam.
  Defer until W1 reliability is measured.

---

## Open questions

- **Deletion / offboarding purge** — confirm the dedup-aware delete path (a content-addressed
  object may back multiple entries or spaces); and forget-me on a *shared-in* space = leave (drop
  own grant) vs. the owner deleting the whole space. Inherit the platform's retention posture;
  don't invent a memory-specific one.
- **Prompt-cache placement** — confirm (A) vs (B) with a spike + `contextBreakdown`; verify the
  shared-index cross-member cache win holds in practice.
- **Assistant / shared conversations** — whose space(s) apply when a conversation is
  assistant-backed or itself shared? Likely: the assistant's bound spaces, resolved against the
  *invoking* user's grants.
- **Indexed-field allowlist** — which frontmatter fields get copied into the manifest `indexed`
  map? Start small (`type`, `status`, `updated`, `commitments.due`/`.open`); expand on evidence.
- **Write-trigger reliability** — measure how often W1 actually fires before committing to W2.
- **Index cap & eviction** — hard cap on index lines; consolidation decides merge vs. drop.
  Start ~150 entries / ~3k tokens.
- **Cross-space linking** — deferred; when does a `[[wikilink]]` ever need to resolve across
  spaces (e.g. a personal space referencing a shared team space)?
- **Concurrency depth** — is optimistic-concurrency on the manifest row enough, or do hot shared
  spaces need per-entry locking / CRDT-ish merge?

## Validation note

Before/while building the read path (B2), walk the repo's **own** coding-agent memory (`MEMORY.md` + per-fact
files + consolidation) **and the live Oliver skill** (`~/Documents/memory/`) end-to-end as the
reference behavior — both are the exact pattern, already proven, and surface the
index-maintenance, entry-typing, and wikilink edge cases early.
