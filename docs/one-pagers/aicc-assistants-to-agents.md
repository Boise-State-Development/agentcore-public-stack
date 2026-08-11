# From "Assistants" to "Agents"

### And why the Agent Marketplace is the governed answer to what we learned

**For:** AI Coordinating Committee · **Date:** August 3, 2026 · **Owner:** Phil Merrell

---

## What we shipped first, and what it taught us

Our first public AI capability let anyone at Boise State create an **Assistant**: a saved set of
instructions, optionally pointed at a knowledge base, shareable by link. It worked. On the legacy
platform, **383 people created 795 Assistants** — real, unprompted adoption from across the
institution.

It also showed us the limits of the model:

- **An Assistant was not the whole thing.** Instructions and one knowledge base — but not tools, not
  skills, not memory, not a governed choice of model. Capability kept arriving as separate features
  with nothing to bind them together.
- **Sharing had no front door.** An Assistant spread by someone sending you a link. There was no way
  to browse what existed, no way to tell a maintained one from an abandoned one, and no way for the
  institution to say "this is the one we stand behind."
- **Publication had no review.** Anything shared was, in practice, published. Nothing checked what
  data it reached, what it claimed to be, or whether it still said what it said when it was shared.

None of that was a failure of the pilot. It was the pilot doing its job.

## The shift: one noun, and it is Agent

We have retired "Assistant." There is now **one concept — the Agent** — and it is a composition of
the platform's governed building blocks: instructions, a **model**, knowledge bases, tools, skills,
and memory. Users compose an Agent in the **Agent Designer**, and they only see the building blocks
their **role** permits. Access is enforced at the role record, not at the resource, so what a person
can bind is the same thing the platform will actually let them run.

This matters to governance for a simple reason: the unit we review, approve, publish, and audit is
now the same unit the runtime executes. There is no gap between the description and the thing.

## The Agent Marketplace: governance made operational

The Marketplace is the institutional shelf — a browse page of published Agents, a detail page worth
reading, and the administrative controls behind it. It deliberately reversed the pilot's posture:

| | Assistants (pilot) | Agent Marketplace (today) |
|---|---|---|
| **Publication** | Self-service via link | **Author submits → administrator approves** |
| **Disclosure** | None | Exposed skills and data bindings shown to the author *before* they submit, and to the reviewer at review |
| **Change after approval** | Invisible | Approved instructions are fingerprinted; a published Agent whose instructions drift is **flagged for re-review** |
| **Removal** | Delete only | **Takedown** as a first-class state, with version history and rollback |
| **Discovery** | Word of mouth | Curated store front, categories, and **role-seeded starting sets** — a student's first session already has the Agents we chose for them |
| **Accountability** | Anonymous | Every listing carries an administrator-managed **publisher** |
| **Reporting** | None | A user report queue, with `inappropriate` on a same-day clock |

**We committed to a service level, because review is only governance if it doesn't become a
bottleneck:** submissions reviewed within **two business days**; `inappropriate` reports **same
day**; all other reports on a **weekly sweep**. A pending count is visible on the administrative
navigation so the queue is seen rather than discovered.

## What this gives the institution

1. **A defensible answer to "who approved this?"** — every published Agent has a named reviewer, a
   recorded approval, and a fingerprint of exactly what was approved.
2. **Curation as a lever, not a hope.** We can promote what's good, seed what's essential by role,
   and take down what isn't — without deleting someone's work.
3. **Access control that actually holds.** Role permissions govern which models, tools, skills, and
   data an Agent may bind, so an approved Agent cannot quietly exceed what its author was entitled
   to.
4. **A path for the good work already out there.** The 795 pilot Assistants aren't discarded — the
   Marketplace is the route by which the best of them become institutionally endorsed.

## Open questions for the committee

- **Who reviews?** Today approval requires full system-administrator rights. A scoped "marketplace
  curator" role is designed but not built — we would like the committee's view on who should hold it.
- **What is the standard for approval?** We have a process; we do not yet have a written rubric.
  Accuracy, scope, data sensitivity, and disclaimer language are the candidate criteria.
- **How far do role-seeded defaults go?** Seeding an Agent to a role puts it in front of every member
  of that role. That is a publishing decision with real reach, and it should have an owner.

---

*Detail: `docs/specs/agent-designer.md` (the Agent record and authoring surface) and
`docs/specs/agent-marketplace.md` (the store, review lifecycle, and curation). Both shipped in full;
usage figures measured against production August 2, 2026.*
