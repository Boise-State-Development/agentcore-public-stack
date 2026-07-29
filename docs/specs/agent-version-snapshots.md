# Agent Version Snapshots — Immutable Approved Listings

**Status:** Design / Proposal
**Author:** (drafted with Claude)
**Date:** 2026-07-29
**Targets branch:** `develop`
**Supersedes:** the post-approval *drift detection* portion of `agent-marketplace.md` (D14)

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
| Agent (draft) | `AGENT#{agentId}` | `PROFILE` (existing) |
| Version | `AGENT#{agentId}` | `VERSION#{n:08d}` |

Zero-padded so `SK` sorts lexically. Versions are written once and never updated —
an immutable record that an admin edit (D13) must not silently rewrite either; see
§6.2.

`listing.publishedVersion` on the Agent item points at the live version. `None` means
nothing is published.

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

`CLAUDE.md` makes prompt-cache stability a contract, and the agent cache keys on
*configuration*. Draft and published are now two distinct configurations for the same
`agentId`, which is correct — but the cache key must include the resolved version, or
promoting a new version will keep serving the old system prompt from a warm agent.
Getting this wrong is a correctness bug that presents as "my approved change didn't
take effect."

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

| PR | Content |
|---|---|
| **PR-1** | `AgentVersion` model + repository (write-once `VERSION#` items), version numbering, snapshot serialization round-trip to `Assistant`. Inert — nothing reads versions yet. |
| **PR-2** | Cut a version on submission; approval promotes it; move GSI5 keys to the version item so the store reads snapshots. Remove `approvedInstructionsHash` / `ListingDrift`. |
| **PR-3** | Invocation resolution: published version for everyone but the owner, draft for the owner, version in the agent cache key. The one seam at `chat/routes.py:1469`. |
| **PR-4** | Lifecycle: `withdrawal_requested` state, delete refusal, admin queue entry for withdrawal requests. |
| **PR-5** | Review diff view (pending vs published). |
| **PR-6** | Publisher management admin page — independent of the rest; can land any time. |

PR-2 and PR-3 must ship in the same release: PR-2 alone makes the store show
snapshots while pinned users still run the draft, which is the confusing half-state.

---

## 8. Open items

- **Version retention.** Every resubmission cuts a version. Unbounded growth is
  probably fine at this scale (a few hundred agents, a handful of versions each), but
  worth a decision rather than a discovery. DynamoDB TTL on superseded versions older
  than N months is the cheap answer — except versions referenced by an audit record
  should survive.
- **Does an admin need to roll back to a prior version?** Falls out nearly free once
  versions are immutable and numbered (repoint `publishedVersion`), and it is the
  obvious answer to "the approved version turned out to be wrong." Not in the phasing
  above; say if it should be.
- **KB / RAG bindings.** A bound knowledge base is referenced by id, and its *content*
  is not snapshottable — re-indexing a KB changes behavior without touching the Agent.
  This spec freezes the binding, not the corpus. Worth stating plainly so the guarantee
  is not oversold: "the Agent's configuration is fixed at approval" is true; "the
  Agent's answers are fixed at approval" is not.
