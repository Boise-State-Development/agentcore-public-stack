# Agent Version Snapshots — Immutable Approved Listings

**Status:** **Complete.** PR-1 (#784) · PR-2 (#787) · PR-3 (#789) · PR-4 (#791) · PR-5 (#793) ·
PR-6 (#795), plus #792 (orphan version rows), #799 (E2E fix pass) and the §8 rollback.
**Author:** (drafted with Claude)
**Date:** 2026-07-29 · revised 2026-07-30 against what shipped
**Targets branch:** `develop`
**Supersedes:** the post-approval *drift detection* portion of `agent-marketplace.md` (D14)

> **This document has been reconciled with the implementation.** Where building it proved
> the design wrong, the section says so inline rather than being silently rewritten — a spec
> that quietly agrees with the code teaches nobody why. The substantive corrections are
> **§4.2** (the agent cache key does *not* need the version, and adding it breaks resume)
> and **§3.3** (the real DynamoDB key prefixes, plus the fail-closed ordering that replaced
> the lost atomicity). §7 carries per-PR status.

---

## 1. Problem

An approved marketplace listing is not a fixed thing. Three findings, all confirmed
in the current code:

**The store serves the live record.** `GET /agents/store`
(`app_api/agent_designer/routes.py:278`) is a sparse GSI5 read over the same
DynamoDB item the author edits. There is no snapshot anywhere in the system.

**Edit and delete guard on ownership only.** `update_assistant`
(`shared/assistants/service.py:458`) and `delete_assistant` (`:864`) verify that
the caller owns the record and nothing else. No listing state is consulted. So the
author of an approved Agent can rewrite its instructions, swap its bindings, or
hard-delete it out of the store.

**The consequence is an invocation problem, not a display problem.** A user who
pinned an approved Agent runs the *live* instructions. A post-approval edit
therefore changes behavior for every pinned user immediately, with no review and
no signal in the store. This is the actual exposure: the store tile being stale is
the least of it.

Today the platform *detects* this after the fact. `AgentListing` records
`approvedInstructionsHash` at approval and the read side derives a `ListingDrift`
marker (`shared/assistants/models.py:28-39`). That is forensics — it tells an admin
that something changed after they approved it, once someone looks. It does not stop
the change reaching users.

Related, and probably unintended: `published → private` is currently in
`AUTHOR_TARGET_STATES` (`shared/assistants/listing.py`), so an author can pull a
live listing unilaterally without an admin seeing it.

---

## 2. Decisions taken (2026-07-29)

Confirmed with the product owner before drafting:

| Question | Decision |
|---|---|
| What does a pinned user run? | **The approved snapshot.** Instructions, bindings, and model settings all come from the reviewed version. This is what makes the feature a control rather than a display fix. |
| Can an author remove a published Agent alone? | **No — admin consent for both** unpublish and delete. Withdrawal becomes a request an admin acts on. |
| Migration of existing listings? | **None.** The marketplace is dev-only and has not been promoted to prod, so this is greenfield: no backfill, no drift-compat, and the data shape can change freely. |

The third decision materially shrinks the work. Everything below assumes dev data
is disposable.

---

## 3. The model

### 3.1 An `AgentVersion` is an immutable snapshot

On **submission**, the reviewable surface of the Agent is copied into a numbered,
write-once version record. Approval promotes that version to published. The author's
live record remains a freely-editable draft that no one else ever sees.

The snapshot captures everything that determines behavior or presentation:

| Field | Why it is in the snapshot |
|---|---|
| `instructions` | The obvious one. |
| `bindings` | Swapping a bound tool or skill changes behavior as much as an instruction edit. Omitting this would leave the hole half-closed. |
| `modelSettings` | Model choice changes cost and answer quality. |
| `name`, `description`, `tagline`, `emoji` / icon | What the reviewer read on the shelf. |
| `starters` | Shown on the launch card; part of the reviewed presentation. |
| `category`, `publisherId` | Reviewer-approved placement and attribution. |

Deliberately **not** in the snapshot: `ownerId`, `visibility`, `status`. Ownership
governs edit rights, and `visibility` is the independent access gate the marketplace
spec is emphatic about keeping separate from `listing.state`. Freezing either into a
version would fuse two axes the codebase has worked to keep apart.

### 3.2 Snapshot at submission, not at approval

The version is cut when the author submits, not when the admin approves.

Taking it at approval leaves a window: the author submits, the admin reads it, the
author edits, the admin approves — and what gets published is not what was read.
That is the same class of bug as the one this spec exists to close, just narrower.
Freezing at submission means the reviewer is always looking at an artifact that
cannot move under them.

Consequence: to change what is under review, the author withdraws the submission and
resubmits. That creates a new version rather than mutating the pending one.

### 3.3 Storage

Same assistants table, alongside the Agent item:

| Item | PK | SK |
|---|---|---|
| Agent (draft) | `AST#{agentId}` | `METADATA` (existing) |
| Version | `AST#{agentId}` | `VERSION#{n:08d}` |

> **Corrected during PR-1 (#784).** This table originally read `AGENT#{agentId}` /
> `PROFILE`. The assistants table has always keyed Agents `AST#{id}` / `METADATA` (see
> `listing_repository._key`), and co-location requires the *same* partition key, so the
> real prefix is what shipped. Only the literal strings were wrong; the design — version
> rows beside the Agent row, sorted — is unchanged.

Zero-padded so `SK` sorts lexically. Versions are written once and never updated —
an immutable record that an admin edit (D13) must not silently rewrite either; see
§6.2. The write is conditional on `attribute_not_exists(PK)`, which does double duty:
it is the immutability guarantee *and* the concurrency guard, since the version
allocator picks its number from a read and a racing submission that already took it
loses the conditional write and re-picks.

`listing.publishedVersion` on the Agent item points at the live version. `None` means
nothing is published. `listing.submittedVersion` points at the version cut at
submission — **added during PR-2 (#787)**, because approval must promote the artifact
the reviewer actually read, and inferring "the latest" breaks the moment anything else
cuts a version mid-review (§6.2 introduces exactly that).

**Placement is the index key, not the snapshot.** An admin may recategorize a live
listing (D13), so the GSI5 partition is derived from `listing.category` and the frozen
`version.category` is never rewritten. Content is immutable; *where it sits* is a fact
about now.

⚠️ **Delisting is no longer atomic, and the invariant is bought with ordering.** The
listing block and its index keys used to be one `update_item`, and that atomicity is
what made "an unpublished agent cannot be in the store" a fact rather than a hope.
Moving the index onto the version row splits it across two items:

```
publish   → write the listing, then write the key   (partial ⇒ recorded, not shelved)
unpublish → clear the key, then write the listing   (partial ⇒ not shelved, recorded live)
```

Both partial outcomes leave the Agent **off** the shelf. The reverse orders leave it
visible while the record says otherwise, which is the single failure the sparse index
exists to prevent. A DynamoDB transaction would restore true atomicity and is the
honest upgrade if fail-closed is ever not enough (see §8).

**The store index moves to the version item.** GSI5 keys are written on the
`VERSION#` row rather than the Agent row, sparse on "this version is the published
one". This keeps the property `listing.py` already prizes — *"unpublication is
enforced by physics rather than by a filter"* — and extends it: the store index
cannot return draft content because draft content has no key in it.

---

## 4. Invocation — the part that matters

Today one call site resolves the Agent for a chat turn:

```python
# apis/inference_api/chat/routes.py:1469
assistant, _ = await get_assistant_with_access_check(...)
...
agent_plan = await resolve_agent_invocation(assistant, current_user)   # :1519
```

`resolve_agent_invocation` takes an `Assistant` object
(`chat/agent_binding_resolver.py:134`). That is the whole seam: if a version
deserializes back into the same `Assistant` shape, choosing *which* `Assistant` to
load is a single well-defined swap and everything downstream — binding resolution,
model access checks, the harness — is untouched.

### 4.1 Which version does a caller get?

| Caller | Gets |
|---|---|
| Anyone who pinned or opened it from the store | The published version |
| The owner, in the Agent Designer or previewing their own draft | The draft |
| An admin reviewing a submission | The pending version under review |

The owner running the draft is not a loophole — it is the only way to iterate before
resubmitting, and it affects nobody else. But it must be explicit and narrow: owner
identity, not "anyone with edit access", and it must be visible in the UI that they
are running an unpublished draft.

### 4.2 Prompt-cache interaction

> ⚠️ **This section was wrong and is corrected here (PR-3, #789).** It originally said the
> agent cache key "must include the resolved version, or promoting a new version will keep
> serving the old system prompt from a warm agent." That is not true in this codebase, and
> acting on it introduces a real bug. The original claim is preserved in this note so the
> reasoning below is legible; **do not re-implement it.**

Two different caches get conflated here, and they need separating.

**The in-process agent cache does not need the version.** `_agent_cache`
(`inference_api/chat/service.py`) holds `BaseAgent` objects keyed on a tuple of
construction **values**, not on row pointers. A version is snapshot *values*; resolution
reads one row instead of another and feeds those values into the same pipeline. Everything
a version changes about behavior already reaches the key:

| Version field | Already in the key as |
|---|---|
| `instructions` | `system_prompt` → `prompt_hash` |
| `bindings` (tool) | `effective_enabled_tools` → `tools_hash` |
| `bindings` (skill) | `effective_skill_ids` → `skills_hash` + `agent_type` |
| `modelSettings` | `effective_model_id` + inference-params hash |
| `bindings` (memory\_space) | becomes `extra_tools` → cache skipped entirely |
| `name`, `tagline`, `emoji`, `starters` | never reach the agent at all |

So a promotion already misses. Adding the version number buys no discrimination.

**And it costs safety.** The resume path rebuilds its cache key from `PausedTurnSnapshot`
— the frozen params of the paused turn — so any new key element the snapshot does not
carry orphans the paused agent under the original key. An OAuth-consent or tool-approval
pause on a published Agent then fails to resume with *"must resume from interrupt"*.
`service.py` warns about precisely this desync. PR-3 added the element, hit that bug, and
threaded `agent_version` through the snapshot, `_construction_snapshot` and
`stream_coordinator` to fix it — five files and a durable schema change to mitigate a
problem created by an element that discriminates nothing. It was reverted; the reasoning
now lives inline at the `resolved_version` declaration in `chat/routes.py`.

**Bedrock prompt caching gets *better*, not worse.** This is the opposite of the risk the
original text implied. Prefix stability depends on the system prompt not changing between
turns of a conversation. Before snapshots, an author saving an edit mid-conversation broke
the prefix for every user mid-turn; now a published Agent's prompt changes only at
approval. Given prod cache write:read is running ~1:2, that is a modest cost win.

What remains true from the original section: promoting a version mid-conversation *does*
break the prefix and force a full re-write for that session. That is inherent — new
instructions are a new prompt — and it is the correct behavior, not something to cache
around.

---

## 5. Lifecycle changes

### 5.1 Withdrawal becomes a request

`published → private` leaves `AUTHOR_TARGET_STATES`. A new state carries the request:

```
published            → withdrawal_requested   author asks to pull the listing
withdrawal_requested → private                admin approves the withdrawal
withdrawal_requested → published              admin declines; listing stays live
```

Requests land in the existing admin review queue next to submissions, so "full
visibility and control" means one queue rather than a second surface to remember.

An author can still take an Agent **private before it is ever published** — the
`private → in_review` and `in_review → private` (withdraw a pending submission)
edges are unchanged. The new constraint applies only once something is live.

### 5.2 Delete is refused while a listing exists

`delete_assistant` gains a listing check and refuses when `listing.state` is anything
other than `private` (or absent). That covers `taken_down` deliberately: an author must
not be able to delete their way out of a takedown record.

The error should name the path forward ("request withdrawal first"), not just refuse.

### 5.3 What this obsoletes

`approvedInstructionsHash`, `ListingDrift`, and the drift markers in the SPA all
exist to detect a condition that becomes structurally impossible. They should be
**removed**, not left dormant — a governance marker that can never fire is worse than
none, because the next reader assumes it is doing something.

This is the portion of `agent-marketplace.md` D14 that this spec supersedes.

---

## 6. Admin surfaces

### 6.1 Review shows a diff

The reviewer's actual question is "what changed since I approved this?" — today they
cannot see it. The review queue should show the pending version against the currently
published one, field by field, with instructions diffed. A resubmission that only
fixes a typo should be approvable in seconds; one that rewrites the instructions
should be obvious.

### 6.2 Admin presentation edits (D13) need a rule

Admins can currently edit a listing's presentation fields in place, appending to
`adminEdits`. Against an immutable version that needs a decision, and the honest
options are:

- **Admin edits cut a new version** (attributed to the admin). Keeps immutability
  absolute; makes a category fix look like a release.
- **Presentation fields stay mutable on the listing**, outside the snapshot, and the
  version freezes only behavior. Simpler, but then the store tile can drift from what
  was approved — reintroducing a smaller version of the original problem.

Recommendation: the first. The version is the unit of "what an admin blessed," and an
admin editing it is still an admin blessing it.

### 6.3 Publisher management UI

`PublisherProfile` (D12) already models exactly the alternative-author-name feature
this work was asked to add — `label`, `kind` (`institution` | `department` |
`individual`), and `verified` for the check mark. Full CRUD plus an eligibility
allowlist is live at `/admin/agents/publishers`, and the SPA already renders publisher
on the store tile and listings page.

**What is missing is the admin page.** There is no `publishers.page.ts` and no
Publishers entry in the admin nav, so a profile like "Registrar" or "Communications &
Marketing" can only be created by calling the API directly.

This is independent of versioning and should ship separately — it is a UI gap over a
finished backend, not a design problem. It needs the `admin.marketplace` scope
(delegated admin, #774) and follows the four-step checklist in
`apis/app_api/admin/README.md`.

---

## 7. Phasing

| PR | Content | Status |
|---|---|---|
| **PR-1** | `AgentVersion` model + repository (write-once `VERSION#` items), version numbering, snapshot serialization round-trip to `Assistant`. Inert — nothing reads versions yet. | ✅ #784 |
| **PR-2** | Cut a version on submission; approval promotes it; move GSI5 keys to the version item so the store reads snapshots. Remove `approvedInstructionsHash` / `ListingDrift`. | ✅ #787 |
| **PR-3** | Invocation resolution: published version for everyone but the owner, draft for the owner. ~~version in the agent cache key~~ (see §4.2 — deliberately not implemented). The one seam at `chat/routes.py`. | #789 |
| **PR-4** | Lifecycle: `withdrawal_requested` state, delete refusal, admin queue entry for withdrawal requests. | |
| **PR-5** | Review diff view (pending vs published). | |
| **PR-6** | Publisher management admin page — independent of the rest; can land any time. | ✅ #795 |
| **follow-up** | E2E fix pass: detail read serves the snapshot, withdrawal decisions reachable, review diff reachable, publisher delete guarded. | ✅ #799 |
| **§8** | Admin rollback to a prior version. | ✅ |

PR-2 and PR-3 must ship in the same release: PR-2 alone makes the store show
snapshots while pinned users still run the draft, which is the confusing half-state.

⚠️ **This is live on `develop` as of #787 merging.** PR-2 is in, PR-3 (#789) is not, so
`develop` — and therefore dev — is currently in exactly that half-state: the store serves
snapshots while pinned users run the draft. It resolves when #789 merges, and nothing
should be promoted to prod until it does.

**One thing PR-2 had to decide that the phasing did not anticipate**: once the store
renders the snapshot, a D13 admin presentation edit lands nowhere unless it cuts a version.
§6.2's first option was therefore not optional, and it shipped — admin edits cut a new
version attributed to the admin.

---

## 8. Open items

- ~~**Version retention.**~~ **DECIDED 2026-07-30: deliberately unbounded.** Every
  resubmission cuts a version and nothing removes one. At this scale — a few hundred
  agents, a handful of versions each, a snapshot being a few KB — the storage is
  immaterial, and the alternatives all cost more than they save. A TTL on superseded
  versions is cheap to run but deletes invisibly and cannot exempt a version an audit or
  takedown record points at; a keep-last-N prune is predictable but silently destroys the
  older half of a listing's approval history, which is the thing the epic exists to make
  durable. **Rollback (below) shipped and made this stronger, not weaker**: an old version
  is no longer inert history, it is something an admin can put back on the shelf, so
  deleting one now costs a recovery path. Revisit if a single Agent ever accumulates
  enough versions to make the partition read slow — that is the symptom worth acting on,
  not the row count.
- ~~**Does an admin need to roll back to a prior version?**~~ **SHIPPED 2026-07-30.**
  `GET /admin/agents/{id}/versions` + `POST /admin/agents/{id}/rollback`, with a picker on
  the admin Listings page. Repoints `publishedVersion` and moves the GSI5 key through
  `_publish_version`, so it inherits the new-key-first ordering. Three constraints worth
  keeping: it acts **only on a `published` listing** (otherwise it is a second door into
  the store, past review); it **requires a reason**, which lands on the author's card the
  way a takedown's does, because an admin changing what users run is not something the
  author should have to discover; and it **cuts no version** — rolling back is a pointer
  move over immutable records, so rolling forward again is the same operation.

  ⚠️ **That last property has to reach the UI, and at first it did not.** The Listings page
  gated its control on `publishedVersion > 1`, which reads "are we serving above the first
  version?" — the same answer as "does a second version exist?" right up until someone rolls
  back, and the opposite one afterwards. A listing rolled back to `v1` still had every later
  snapshot intact and the endpoint would happily repoint at one, but the only entry point was
  hidden: the rollback could not be undone from the UI. The row now carries `latestVersion`
  (the high-water mark, which survives the pointer moving down) and the control, its label
  and the dialog's copy are all direction-neutral, because half of this feature's uses are
  the second half of an earlier use.
- **Transaction vs fail-closed ordering.** Publishing and delisting are now two writes on
  two items (§3.3), and the "an unpublished agent cannot be in the store" invariant is held
  by call order rather than by atomicity. Ordering is enforced in one place
  (`listing_service._unindex_version`) and asserted by test, but it lives in a docstring
  rather than in the type system: a future delisting path that writes the record before
  clearing the key breaks it quietly. `TransactWriteItems` over the two items would make it
  structural.
- **Agent-bound MCP-app dispatch builds its own agent.** Two call sites in `chat/routes.py`
  construct agents from `input_data` rather than from the resolved assistant, so their cache
  keys already diverge from the main turn's on an agent-bound session. Pre-existing, not
  caused by this spec, and the same desync family as the resume case in §4.2 — but nobody
  owns it.
- **KB / RAG bindings.** A bound knowledge base is referenced by id, and its *content*
  is not snapshottable — re-indexing a KB changes behavior without touching the Agent.
  This spec freezes the binding, not the corpus. Worth stating plainly so the guarantee
  is not oversold: "the Agent's configuration is fixed at approval" is true; "the
  Agent's answers are fixed at approval" is not.
