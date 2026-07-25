# Agent Directory — a browse-and-discover surface for published Agents

> **⚠️ Superseded (2026-07-24) by [`agent-marketplace.md`](agent-marketplace.md).**
> That spec carries forward D1 (one noun), D2 (sparse index), D3 (`listing` separate from
> `visibility`) and D6 (pin, never fork), and **replaces** D4 (runnability placement) and D7
> (self-service publication — publishing is now admin-gated). This document is kept for the
> reasoning behind the carried-forward decisions; build from the marketplace spec.

**Status:** Superseded — see above. Originally Draft / proposal (2026-07-18)
**Author:** Phil Merrell (drafted with Claude)
**Targets branch:** `develop`
**Depends on:** [`agent-designer.md`](agent-designer.md) Phases 1–2 (the Agent record + the
bindable catalog) — shipped.
**Resolves:** the "per-invoker capability preview" open question in
[`agent-designer.md`](agent-designer.md) (D4 below).
**Related:** agentic-platform primitives F6b Registry
([`agentic-platform-primitives.md`](agentic-platform-primitives.md)); Skills v2 invoke-through
sharing ([`skills-as-agent-primitive.md`](skills-as-agent-primitive.md) §6/D7); assistant sharing /
collaborative editing (issue #113).

## Summary

Give users a **directory** — a browse page and a detail page — where they discover Agents other
people have published, understand what an Agent does and what it can reach, see whether it will
actually run *for them*, and start a conversation with it.

The Agent Designer answered *how do I build one*. Nothing answers *how do I find one someone else
built*. Today the only discovery affordance in the product is a "Shared with me" grouping inside the
list page ([`assistant-list.component.html:26`](../../frontend/ai.client/src/app/assistants/components/assistant-list.component.html)),
which requires the author to already know your email address. That makes every Agent a private
artifact and every good Agent a thing you hear about by word of mouth — the opposite of the
dogfooding flywheel the primitives epic is built around (`agentic-platform-primitives.md` §5).

This spec adds **no new primitive**. It is a surface over the Agent record that already exists.

---

## D1 — One noun: Agent. We do not introduce "Plugin"

Both Claude and ChatGPT ship a three-layer stack: a **capability** layer (skills, connectors, MCP
apps), a **bundle** layer that packages capabilities for distribution (both now call this a
"plugin"), and a **persona** layer you converse with (Projects, Custom GPTs). They need the bundle
layer because their capability layer is installed *into a workspace*, separately from any persona.

**Our Agent already is the bundle.** `bindings[]` — `knowledge_base | tool | skill | memory_space`
over one governed `modelConfig` — is precisely what a vendor plugin packages, and it is already
attached to the persona rather than floating beside it. Introducing a second "Plugin" noun would ask
users to learn a distinction our data model does not make, and would fragment one binding surface
into two.

The directory is therefore a **view over Agents**, not a store of a new kind of thing. The vendor
plugin-detail *layout* is worth copying almost field-for-field; the vendor plugin *entity* is not:

| Vendor plugin-detail field | Our Agent field | Status |
|---|---|---|
| Icon, name, publisher | `emoji`, `name`, `ownerName` | exists |
| Description | `description` | exists |
| Example prompts | `starters[]` | exists |
| "What it can access" | `bindings[]` | exists |
| Category filters | `tags[]` + `listing.category` | tags exist |
| Install button | "Start chat" / pin (D6) | new |

Every field but the last two already exists on the record. This is a read-view build, not a
data-model build.

**When "Plugin" becomes a real question:** if users start assembling the *same* tool+skill+KB
combination onto three or more Agents, a reusable binding bundle earns its own noun. That is exactly
the "Skill vs. Agent reconciliation" question parked in `agent-designer.md`, and `bindings[].kind`
is an open enum specifically so the answer stays additive. Do not pre-build it. The directory will
produce the evidence — a bundle that gets re-bound repeatedly is visible in the listing data.

## D2 — Publication is a sparse index, and the read path mostly exists

The `rag-assistants` table already carries a `VisibilityStatusIndex` (GSI2,
`GSI2_PK = VISIBILITY#{visibility}`), defined in
[`rag-data-construct.ts:142`](../../infrastructure/lib/constructs/rag/rag-data-construct.ts) and
written on every put and update ([`service.py:174-177`](../../backend/src/apis/shared/assistants/service.py)).
`PUBLIC` **access** also still resolves — a non-owner opening a PUBLIC agent gets `"viewer"`
([`service.py:265`](../../backend/src/apis/shared/assistants/service.py)).

What was removed is only the *listing*: `list_user_assistants` force-sets `include_public = False`
with the comment *"the feature is removed"* ([`service.py:672`](../../backend/src/apis/shared/assistants/service.py)),
disabled in `ad4437e9` when explicit email sharing superseded a public index. So the plumbing is
live and populated; the query was switched off, not the infrastructure.

We do **not** simply re-enable that query, for two reasons: `VISIBILITY#PUBLIC` is a single hot
partition holding every public agent forever, and it cannot be filtered by category without a scan.
Instead add a **sparse `AgentDirectoryIndex` (GSI5), written only when an agent is listed**:

```
GSI5_PK = LISTED#{category}      # sparse — absent unless listing.listed
GSI5_SK = CREATED#{created_at}   # stable pagination key
```

GSI5 is the next free slot on `rag-assistants`: GSI_/GSI2/GSI3/GSI4 are `OwnerStatusIndex`,
`VisibilityStatusIndex`, `SharedWithIndex`, and `DueSyncIndex` respectively. `DueSyncIndex` is the
direct precedent — a sparse index on the *same table* whose keys exist only on `SYNCPOL#` items in
the active state, so the dispatcher's query physically cannot see paused policies
([`rag-data-construct.ts:154-163`](../../infrastructure/lib/constructs/rag/rag-data-construct.ts)).
Same trick here: an unlisted agent has no GSI5 key, so the directory query cannot return it —
unpublication is enforced by physics, not by a filter someone can forget. Category-partitioning also
gives the browse page's chips a direct query instead of a filtered scan.

**Ranking caveat, stated plainly:** `GSI5_SK` is `created_at`, so the directory is **newest-first**.
Sorting by popularity needs a mutable sort key (a hot-item rewrite on every use) or a periodic
recompute; v1 does neither. `usage_count` is returned and rendered as a signal, and "featured"
(D7) is the manual override. A real popularity ranking is deferred to F6b, not silently approximated.

## D3 — `listed` is separate from `visibility`; existing PUBLIC agents are not auto-listed

It is tempting to make `visibility == 'PUBLIC'` mean "in the directory." That is wrong, and it is
the one decision here with a data-safety consequence.

`PUBLIC` today means *anyone with the link may use this* — the share dialog explicitly surfaces a
shareable URL for PUBLIC assistants
([`share-assistant-dialog.component.ts:39`](../../frontend/ai.client/src/app/assistants/components/share-assistant-dialog.component.ts)).
Unlisted-but-linkable is a legitimate, already-supported mode, and users have been choosing `PUBLIC`
under those semantics for as long as the option has existed. If listing were derived from
visibility, shipping this spec would **retroactively publish every existing PUBLIC agent to the
whole institution**, with no author consent and no review.

So: `visibility` stays the **access gate**; a separate `listing` block is the **publication state**.
Backfill is `listed: false` for every existing record — publication becomes an explicit forward act
by each author, never an inference from past configuration.

This also gives publication somewhere to put the metadata visibility cannot carry: category,
publication timestamp, featured flag, takedown state.

Note the live gap this closes: `PUBLIC` is already an option in the Agent form
([`agent-form.page.html:104`](../../frontend/ai.client/src/app/agents/agent-form/agent-form.page.html))
and nothing in the product surfaces the result. Today it is a dead-end dropdown.

## D4 — The detail page previews runnability; it does not promise it

`agent-designer.md` D5 sets a **block-on-missing** policy: every gated capability re-resolves
against the *invoking* user at run time, and a missing one blocks with a message. That rule is
right, and a directory strains it. Share-by-email means the author knows who the sharee is; a
directory means anyone can open anything. Block-on-missing plus open browsing equals a catalog where
an unknown share of entries fail on first message.

The fix is not to weaken D5 — it is to **tell the user before they start**. The detail page
resolves the Agent's `modelConfig` + `bindings` against the viewer's own
`GET /agents/bindable` results (already RBAC-filtered per D4 of the Designer spec) and renders one
of three states:

- **Ready** — every binding resolves for you.
- **Runs with limits** — an optional binding is unavailable; the agent still starts.
- **Not available to you** — a required binding (model, restricted tool, memory space) does not
  resolve; name what is missing and who to ask.

This is the "per-invoker capability preview" that `agent-designer.md` parks as an open question. A
directory forces it: it is the difference between a catalog and a minefield. The computation is a
set-difference against an endpoint that already exists — no new access logic (Designer D4 stands: we
compose the five per-primitive checks, we do not invent a sixth).

## D5 — Publishing amplifies invoke-through, so publishing must disclose it

Skills v2 D7 makes skill sharing **invoke-through**: a `skill` binding resolves when
`skill.owner_id == agent.owner_id` and the invoker has access to the Agent — the owner-match clause
is what prevents chain-sharing
([`access.py`](../../backend/src/apis/shared/skills/access.py) module docstring).

Under email sharing, "the invoker has access" is a list the author typed. Listing an Agent changes
that population to *everyone*, which means **publishing an Agent effectively publishes the contents
of every skill its author wrote and bound to it**. That is a correct consequence of D7 and not a bug
— but it must be an *informed* act, not a side effect of a toggle.

Requirements:

1. The publish dialog **enumerates** what publication exposes: "N skills you authored will be
   readable by anyone who runs this agent," listed by name.
2. `memory_space` bindings **block publication.** A memory space is personal data; D5's re-resolve
   already denies a sharee who lacks the space, so a published agent bound to one is a guaranteed
   "Not available to you" for every viewer *and* an invitation to leak by future policy drift.
   Refuse at publish time with a clear message rather than shipping a listing that cannot run.
3. Unlisting is immediate and revokes nothing retroactively — sessions already started continue.
   Say so in the UI; do not imply unlisting is a recall.

## D6 — No install, no fork. "Add to my agents" is a pin

Vendors need an install step because their bundle must be added to a workspace before it is usable.
Ours are addressable by id and already run for anyone with access — there is nothing to install.

The install button therefore degrades to a **pin**: the Agent appears in your `/agents` list under a
"Pinned" group, and disappears when unpinned. No copy is made.

**Explicitly not copy-on-install.** Forking a published Agent into the user's own store would
duplicate the record (version drift the moment the author updates it), re-owner the bindings (which
breaks invoke-through in the confusing direction — the fork's new owner does not own the skills, so
they silently stop resolving), and multiply takedown surface. If users want a variant, they can
build one in the Designer; that is what the Designer is for.

---

## Data model

Additive to the existing Agent record (`apis/shared/assistants/models.py`) — no new table, per
Designer D2:

```
Agent {
  ...                                   # unchanged
  listing?: AgentListing                # absent = not published (the backfill default, D3)
}

AgentListing {
  listed:     bool                      # drives the sparse GSI5 write (D2)
  category:   str                       # directory partition; from a curated set (open question)
  listedAt:   str                       # ISO 8601
  listedBy:   str                       # ownerId at publication time
  featured:   bool = false              # admin-set (D7); ordering override only
  takedown?:  { by, at, reason }        # admin unlist; blocks re-publish until cleared
}
```

`pinned` is **not** on the Agent — it is per-user. Store pins on the user record / a small
`AGENT_PIN#{userId}` item; a pin is one user's view state, never a mutation of someone else's agent.

**Directory read shape.** `AgentResponse` carries `instructions`, and the directory must not.
A published Agent's system prompt is the author's work; browsing a catalog should not dump it. Add
an `AgentListingResponse` — `agentId, name, description, emoji, ownerName, tags, category,
usageCount, featured, bindingSummary[]` — where `bindingSummary` is `{kind, label}` per binding
(what it can reach, not the ids). No `instructions`, no `ref` values.

⚠️ **Behavior change to decide (open question):** `GET /agents/{id}` today returns full
`AgentResponse` *including* `instructions` to a PUBLIC viewer. Once agents are broadly listed, that
is a much larger exposure than it was under link-sharing. Recommend gating `instructions` to
`permission in ("owner", "editor")` on the detail read.

## APIs / contracts

All SPA-facing, `Depends(get_current_user_from_session)` per the CLAUDE.md app-api rule, all behind
the D8 gate.

| Route | Purpose |
|---|---|
| `GET /agents/directory?category=&cursor=` | Browse. Sparse-GSI query → `AgentListingResponse[]` + cursor. Newest-first (D2). |
| `GET /agents/directory/categories` | Category chips + counts for the browse header. |
| `GET /agents/{id}` | Detail. Existing route; add `listing` + the `instructions` gate. |
| `GET /agents/{id}/runnability` | Per-invoker capability preview (D4) → `{state, missing[]}`. |
| `POST /agents/{id}/listing` | Publish (owner only). Runs the D5 disclosure checks; 400 on a `memory_space` binding. |
| `DELETE /agents/{id}/listing` | Unlist (owner or admin). |
| `POST /agents/{id}/pin` / `DELETE` | Add to / remove from my agents (D6). |
| `GET /admin/agents/listings` | Admin curation queue (D7). Under `/admin/` per the route convention. |
| `PATCH /admin/agents/listings/{id}` | Feature / unfeature / take down. |

`GET /agents/{id}/runnability` composes `bindable_catalog.list_bindable` per kind and diffs against
the agent's bindings — it introduces no new access service (Designer D4).

## D7 — Curation is admin-light: feature and take down, no pre-publish review

A review queue gates publication on staff attention, which for an internal platform means
publication effectively stops. Publish is **self-service and immediate**; admins get an ordering lever
(`featured`) and a remedy (`takedown`). The population is authenticated institutional users under
existing RBAC — the governance posture the platform already inherits
(`feedback_governance_via_identity_claims`), not an open marketplace.

Takedown sets `listing.takedown` and clears the sparse GSI key, so the agent stays reachable by
direct link (visibility is unchanged — D3) but leaves the directory.

## D8 — Ship gated, GA by grant

House cross-cutting pattern (`agentic-platform-primitives.md` §4): kill switch
`AGENT_DIRECTORY_ENABLED` (**default on**, `=false` disables — per the feature-flag convention) plus
an RBAC capability `agent-directory` that 404s the routes for ungranted roles, mirroring the
`skills` gate from Skills v2 PR-5. Ships to prod scoped; GA is one role grant, no redeploy.

## Frontend

New route `/directory`, plus `/directory/:agentId`.

**Browse** — search box, category chips, a featured row, then a card grid: emoji, name, owner,
one-line description, `usageCount`, and a runnability dot. Cursor pagination. Search in v1 is a
substring match over `name`/`description`/`tags` **within the loaded page** — a real index is F6b.
Label it honestly ("searching loaded results") rather than implying full-corpus search.

**Detail** — the vendor layout mapped to our fields (D1): header (emoji, name, owner, category,
Start chat, pin), description, **"Try asking"** rendered from `starters[]`, **"What this agent
uses"** rendered from `bindingSummary[]` grouped by kind, and the runnability banner (D4) at the top
when the state is anything but Ready.

Reuse the `redesign-tokens` list-page idiom (`rounded-2xl` / `text-sm`, flat, no heavy section
cards), `@angular/cdk/dialog` for the publish dialog, signal-based state, and the existing tooltip
requirement on icon-only buttons.

## Phasing

```
Phase 0  This spec
Phase 1  listing block + sparse GSI5 + publish/unlist API + backfill listed=false (D2/D3)
Phase 2  GET /agents/directory + the browse page                                  ← the surface
Phase 3  Detail page + GET /{id}/runnability + the instructions gate (D4)
Phase 4  Pin / "Add to my agents" (D6)
Phase 5  Admin curation: feature + takedown (D7)
Later    Ranking + full-corpus search via the Registry catalog (F6b)
```

Phases 2 and 3 are the user-visible payoff and could ship together; 4 and 5 are independent and
parallelizable.

## Non-goals (v1)

- **No "Plugin" primitive** (D1). One noun.
- **No install / fork / copy** (D6). Pin only.
- **No pre-publish review workflow** (D7). Self-service publish, admin takedown.
- **No ratings, reviews, or comments.** Social proof is `usageCount` + `featured`.
- **No popularity ranking** (D2). Newest-first, honestly labeled.
- **No cross-tenant or external publishing.** The directory is institution-scoped.
- **No agent versioning or changelogs.** A published Agent is a live pointer; updates are immediate.

## Open questions

- **Instructions visibility.** Should a non-owner viewing a listed Agent see its `instructions`?
  Recommended: no (gate to owner/editor) — but this changes today's PUBLIC-viewer behavior and needs
  a call.
- **Category taxonomy.** A curated fixed set (clean chips, needs governance) or promoted `tags[]`
  (zero admin, messy)? Recommend a small fixed set for v1 with tags as free-text underneath.
- **Pin vs. usage in ranking.** Does a pin count toward `usageCount`, or is it a separate signal?
  They mean different things (intent vs. traffic) and conflating them makes both useless.
- **Quota attribution.** Confirm the invoker pays for a directory-launched run, not the author —
  the existing per-user model implies yes, but a published Agent is the first case where a stranger's
  usage is attributable to someone else's configuration.
