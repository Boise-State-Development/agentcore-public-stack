# Per-User Markdown Memory (Karpathy-style "second brain" on S3)

**Status:** Draft / proposal
**Author:** (drafted with Claude)
**Date:** 2026-06-27
**Targets branch:** `develop`
**Related:** skills reference-file / progressive-disclosure pattern (`agents/main_agent/skills/`, `apis/shared/skills/resource_store.py`), AgentCore Memory write-only limitation (`agents/main_agent/session/turn_based_session_manager.py`), context attribution (`apis/shared/costs/`)

## Summary

Give every user a small, **human-readable markdown memory** that the agent reads at the
start of a conversation and maintains over time — a per-user "second brain" backed by S3
instead of a local filesystem. The shape is the one Karpathy popularized and the one this
very repo's coding agent uses internally: a tiny always-loaded **index** (`MEMORY.md`) of
one-line pointers, plus a set of **one-fact-per-file** markdown notes fetched on demand.

The architectural move is that we already ship this mechanism — it's the **skills
reference-file / progressive-disclosure** path ([`skill_registry.read_resource`](../../backend/src/agents/main_agent/skills/skill_registry.py),
[`SkillResourceStore`](../../backend/src/apis/shared/skills/resource_store.py)): S3
content-addressed storage, a lightweight DynamoDB manifest, **server-side read
mid-conversation**, and a Level-1 catalog injected into the system prompt with Level-2+
files fetched via a tool. Per-user memory is that mechanism **re-scoped from per-skill to
per-user**, plus a write/consolidation path.

This is the right design (vs. leaning on AgentCore Memory) because **AgentCore Memory is
write-only in cloud** — the SDK restore branch never fires, so Memory cannot be the
read-time source of truth today (see
[`project_session_restore_writeonly_memory`](../../backend/src/agents/main_agent/session/turn_based_session_manager.py)
analysis). An S3 markdown layer fills a real gap rather than competing with a working
system.

### Prior art

- **Karpathy's LLM Wiki / second brain** — agent-maintained markdown wiki: immutable raw
  sources, an AI-generated/-maintained wiki layer, and a schema file governing read/write/
  reconcile. Core claim: LLMs don't get bored, so the wiki-maintenance burden that kills
  human wikis disappears. ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f))
- **MRAgent — "Memory is Reconstructed, Not Retrieved"** ([arxiv 2606.06036](https://arxiv.org/abs/2606.06036),
  [repo](https://github.com/Ji-shuo/MRAgent)) — interleaves LLM reasoning with memory access
  (active reconstruction) rather than a static retrieve-then-reason step. The transferable
  lesson: **don't pre-stuff context; let the agent fetch on demand inside its loop.**
- **This repo's own coding-agent memory** — `MEMORY.md` index + one-fact-per-file markdown
  with frontmatter (`name`/`description`/`type`), `[[wikilinks]]` between facts, a
  consolidation pass. The cleanest reference implementation of exactly what we'd build.

## Goals

- A per-user, durable, **human-readable** markdown memory the agent reads at conversation
  start and updates over time.
- **Token-bounded**: steady-state cost is the index only; fact bodies are lazy.
- Reuse the **skills reference-file mechanism** (S3 store + DynamoDB manifest + on-demand
  read tool) re-scoped to `USER#{user_id}`, with minimal new infrastructure.
- **User-visible & user-editable**: "here's what I remember about you," with delete/export.
  This is a feature, not overhead — and likely a compliance requirement.
- Ship **dark behind a flag** (`USER_MEMORY_ENABLED`, default off), per-environment enable,
  mirroring `SKILLS_ENABLED`.

## Non-goals (v1)

- A graph/vector memory (MRAgent's full Cue–Tag–Content graph). v1 is flat markdown +
  an index; the *reconstruction* lesson is adopted (on-demand fetch), the graph is not.
- Importing raw source documents into a wiki (Karpathy's "raw sources" layer). v1 memory
  is distilled facts, not a document corpus — that overlaps the existing RAG/assistant KB.
- Cross-user / org-shared memory. Memory is strictly `USER#{user_id}`-scoped.
- Memory inside assistant-backed or shared conversations (see [Open questions](#open-questions)).
- Replacing AgentCore Memory or the compaction/summary path — this is additive.

---

## Background: the pattern we're extending

Skills already do per-skill progressive disclosure end-to-end. The whole of this spec is
re-pointing it at users and adding writes.

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
- Per-user DynamoDB scoping: `PK=USER#{user_id}, SK=...` (sessions, artifacts, preferences)
- Token accounting we'll reuse: `contextBreakdown` partitions in [`costs/models.py`](../../backend/src/apis/shared/costs/models.py)

**The asymmetry that matters:** skill resources are **read-only and admin-authored**;
user memory is **read-write and agent/user-authored**. The store, manifest, and read tool
transfer directly. The new work is the **write + consolidation** path and the
**prompt-cache-safe injection** of the index.

---

## Design

### 1. Storage layout (S3 + DynamoDB manifest)

Reuse the skill-resources bucket pattern under a per-user prefix (or a dedicated
`user-memory` bucket — see [CDK](#5-infrastructure)):

```
memory/users/{user_id}/MEMORY.md              # the index — small, always-loaded
memory/users/{user_id}/facts/{slug}.md        # one fact per file, fetched on demand
```

Each fact file carries frontmatter (mirrors the coding-agent memory shape):

```markdown
---
name: prefers-concise-answers
description: User wants terse, direct responses; skip preamble
type: user | feedback | project | reference
updated: 2026-06-27
---

User has repeatedly asked for shorter answers... Link related facts with [[other-slug]].
```

Manifest: a small DynamoDB row `PK=USER#{user_id}, SK=MEMORY#INDEX` holding the file list
(`slug`, `description`, `content_hash`, `size`, `updated`) — structurally a list of
`SkillResourceRef`. **No bodies in DynamoDB** (400 KB item-limit rule, same reason skills
went to S3). The index `MEMORY.md` itself lives in S3; the manifest row is the
fast-path pointer + cache-key input.

### 2. New shared service: `UserMemoryStore`

New package `apis/shared/memory/` (a shared concern: both app-api and inference-api
consume it, so it lives in `apis.shared` per the import-boundary rule).

```python
# apis/shared/memory/store.py
class UserMemoryStore:
    def read_index(self, user_id: str) -> str: ...                  # MEMORY.md text
    def list_facts(self, user_id: str) -> list[MemoryFactRef]: ...  # manifest, no bodies
    def read_fact(self, user_id: str, slug: str) -> str: ...        # one fact body
    def write_fact(self, user_id: str, slug: str, body: str) -> MemoryFactRef: ...
    def update_index(self, user_id: str, body: str) -> None: ...
    def delete_fact(self, user_id: str, slug: str) -> None: ...
```

Mirror `SkillResourceStore`: lazy boto3 init, content-addressed objects, raise loudly on
miss (no silent failures), best-effort delete. Env var `S3_USER_MEMORY_BUCKET_NAME`.

### 3. Read path — index in prompt, facts on demand

**Index injection (Level 1).** At conversation start, inject `MEMORY.md` into the system
prompt as a bounded block. This is the MRAgent discipline applied: only the index is
pre-loaded; fact bodies are reconstructed on demand.

> **Prompt-cache constraint (decide here, not later).** Per-user content in the system
> prefix gives each user a distinct cache key and interacts badly with the shared-prefix
> caching [`system_prompt_resolver.py`](../../backend/src/apis/inference_api/chat/system_prompt_resolver.py)
> relies on (it deliberately *gates* injection on continuation/prompt-cache turns).
> Two viable strategies:
> - **(A) Dedicated cache block.** Append the memory index as its own
>   `cache_control` breakpoint after the shared platform prefix, so the platform prefix
>   stays globally cached and the per-user index caches *within* that user's session.
>   Preferred — keeps both caches working.
> - **(B) Tool-loaded on turn 1.** Skip the prefix entirely; the agent calls a
>   `memory_index()` tool on the first turn. Zero prefix-cache disruption, costs one
>   extra tool round-trip per conversation.
>
> Recommend **(A)**; spike both and measure with `contextBreakdown`.

**Fact fetch (Level 2).** A `memory_read(slug)` tool fetches one fact body server-side
from S3 — a direct analog of `read_resource`. The agent fetches only what the current
turn needs.

### 4. Write path

Two mechanisms, not mutually exclusive:

- **(W1) Synchronous agentic write** — `memory_write(slug, body)` / `memory_update(slug, body)`
  tools the model calls mid-turn when it learns something durable, plus an index-update
  step. Transparent, simple, matches the coding-agent memory model. Risk: relies on the
  model remembering to write. **v1 default.**
- **(W2) Async post-turn reflection** — a background job reads the completed turn and
  proposes memory edits, hung off the existing post-turn hook in
  [`turn_based_session_manager.update_after_turn`](../../backend/src/agents/main_agent/session/turn_based_session_manager.py)
  (same seam compaction uses). Reliable, no reliance on in-loop discipline, but adds an
  LLM call per turn. **Phase 2.**

**Consolidation.** A periodic pass (Karpathy's core insight — LLMs don't get bored) that
merges duplicate facts, fixes stale ones, prunes the index, and enforces the index cap.
Run as a scheduled job per active user (or lazily, when the index crosses a size
threshold). Mirrors the repo's `consolidate-memory` skill conceptually.

### 5. Infrastructure

New `UserMemoryConstruct` (clone of [`skill-resources-construct.ts`](../../infrastructure/lib/constructs/skills/skill-resources-construct.ts)):
S3 bucket (AES256 or **CMK — see PII below**), block public access, enforce SSL, no
auto-expiry (memory is durable; deletion is explicit/user-driven). Thread the bucket to
compute roles via `PlatformComputeRefs` (typed ref, not SSM), per the construct rules.
Read+write grant to inference-api runtime role and app-api role.

### 6. User-facing surface (app-api + SPA)

Because memory is human-readable markdown, expose it. `app-api` routes under
`/memory/` (user-facing, `Depends(get_current_user_from_session)` — **not** Bearer, per
the auth-dependency rule):

- `GET /memory` — list facts (manifest) + index
- `GET /memory/{slug}` — read one fact
- `PUT /memory/{slug}` / `DELETE /memory/{slug}` — user edits/deletes a fact
- `DELETE /memory` — wipe (compliance "forget me")

SPA: a "What I remember about you" panel — view, edit, delete, export. This is the
differentiator vector RAG can't offer and the FERPA control surface.

---

## Token efficiency analysis

The model's headline advantage, and it's quantifiable.

- **Steady-state overhead = index only.** ~1 line (~15–25 tokens) per fact. 50 facts ≈
  **~1k tokens/turn**; 200 facts ≈ **~4k tokens/turn**. Bounded and tunable via the
  consolidation cap. Fact bodies cost **zero** unless fetched.
- **Lazy bodies (MRAgent discipline).** You pay a fact's body tokens only on turns where
  the agent fetches it, not every turn.
- **Versus alternatives:**
  - *Transcript replay* (today's agent-cache continuity): unbounded, grows every turn —
    exactly what compaction fights.
  - *Vector RAG*: pays embedding + opaque chunk injection per query; not user-inspectable.
  - *AgentCore summaries*: unusable for read in cloud (write-only path).
- **Measurement.** Add a `memory` partition to `contextBreakdown` so the index cost is
  visible per turn and we can validate the cap empirically (same method the skills PR-7
  used to confirm `toolTokens` dropped).

**Net:** capped index + lazy fetch ⇒ **~1–4k tokens/turn steady-state**, far cheaper and
more controllable than replay or RAG. The one cost risk is prompt-cache disruption, fully
mitigated by strategy (A) above.

---

## Phasing (proposed PRs)

- **PR-1 — Data layer.** `apis/shared/memory/` (`UserMemoryStore` + `MemoryFactRef` model +
  manifest repo). `UserMemoryConstruct` (CDK). Unit tests. No runtime wiring. Flag added,
  default off.
- **PR-2 — Read path.** Index injection (strategy A) into the system prompt + `memory_read`
  tool + `memory` partition in `contextBreakdown`. Gated by `USER_MEMORY_ENABLED`.
- **PR-3 — Write path (agentic, W1).** `memory_write` / `memory_update` tools + index
  maintenance. Round-trips through the existing tool RBAC/registry.
- **PR-4 — User surface.** `app-api` `/memory/` CRUD + SPA "What I remember" panel
  (view/edit/delete/export).
- **PR-5 — Consolidation.** Scheduled/threshold consolidation pass + index cap enforcement.
- **PR-6 — Reflection writes (W2), optional.** Post-turn reflection job on the
  `update_after_turn` seam. Defer until W1 reliability is measured.

---

## Open questions

- **FERPA / student PII (biggest risk).** Durable plaintext per-user memory of an academic
  chatbot will capture sensitive student data. Need a position *before code*: CMK
  encryption, retention/TTL policy, a write-side redaction/allowlist policy, and the
  "forget me" wipe. This shapes the design more than the storage mechanics.
- **Prompt-cache placement** — confirm strategy (A) vs (B) with a spike + `contextBreakdown`.
- **Assistant / shared conversations** — whose memory applies, if any? Likely "user memory
  off when assistant-backed" in v1 (matches skills-mode exclusion precedent).
- **Write-trigger reliability** — measure how often W1 actually fires before committing to
  whether W2 is needed.
- **Index cap & eviction policy** — hard cap on index lines; consolidation decides what to
  merge vs. drop. What's the cap? (Start ~150 facts / ~3k tokens.)
- **Multi-device / concurrency** — two concurrent sessions writing memory. Content-addressed
  writes + last-write-wins on the index, or optimistic-concurrency on the manifest row?

## Validation note

Before/while building PR-2, walk the repo's **own** coding-agent memory
(`MEMORY.md` + per-fact files + consolidation) end-to-end as the reference behavior — it is
the exact pattern, already proven, and surfaces the index-maintenance and wikilink edge
cases early.
