# The gap behind the eleven: content, not capability

### What Vandalizer ships that G1–G11 never names

**For:** engineering · **Date:** October 20, 2026 · **Owner:** Phil Merrell
**Status:** observation from a code read of Vandalizer (`ui-insight/vandalizer`,
GPL-3, public) on 2026-10-20. Not a spec and not a plan of record — it names
one thing the parity analysis structurally could not see, and says what would
close it. Anything here that disagrees with a spec loses to the spec.

*Companions:* `docs/specs/structured-extraction.md` (G1, and §15 where the
"G5" label is retired) · `docs/specs/source-provenance-verification.md` (G2) ·
`docs/examples/skills/funder-family/references/reduce-across-documents.md`
(the one piece of this content we have).

---

## The observation

A gap analysis of Vandalizer — the research-administration platform built at the
University of Idaho under NSF GRANTED, and the implementation G1 and G2 were
ported from — produced eleven items, G1–G11:

| | |
|---|---|
| G1 | Structured extraction as a platform primitive |
| G2 | Per-field provenance verification |
| G3 | OCR and page-image fallback |
| G4 | Agent evaluation and tuning as a product surface |
| G5 | Batch fan-out over documents *(label retired — `structured-extraction.md` §15)* |
| G6 | Event-triggered agent runs |
| G7 | A review queue over results |
| G8 | Template and form output |
| G9 | Cross-field validation rules |
| G10 | Runtime branding |
| G11 | Deployment portability |

Every one of them is a **platform primitive**. That is what a capability
comparison between two platforms produces, and as far as it goes it was right:
G1 and G2 are built, merged and demonstrated against a real solicitation, and
G5 turned out to be two different jobs under one name.

What no row names is the thing that makes Vandalizer useful to a grants office
**on the day it is installed**, and it is not a capability:

> **It ships the office's accumulated knowledge as seed data** — a library of
> named, lifecycle-specific document jobs, and the regulatory corpora those
> jobs are checked against.

## What they ship

Counted from their seed tree:

* **21 workflow seeds**, filed under four lifecycle collections (pre-award,
  post-award, compliance, financial) plus two reference collections. They are
  not demos. They are the named jobs of a sponsored-programs office: FOA and
  RFA checklists, proposal readiness review, sponsor compliance checking,
  budget analysis, notice-of-award summary and risk analysis, prior-approval
  tracking, subaward extraction from multi-attachment packages, SF-425
  financial reporting, closeout checklist generation, effort reporting, F&A
  rate validation, cost allocation against 2 CFR 200, COI review, export
  control screening against EAR/ITAR/OFAC, IRB protocol pre-review. Several
  carry authorship credit to practitioners at the originating institution.
* **11 knowledge-base seeds** — the authorities those workflows cite: NSF
  PAPPG, NIH Grants Policy Statement, the Uniform Guidance, the FAR, the Common
  Rule, export control, financial conflict of interest, responsible conduct of
  research, DOD and DOE funding policy, and the institution's own sponsored
  programs policies.
* **A benchmark corpus with an answer key** — a synthetic 16-document NSF
  proposal packet (narrative, budget justification, budget workbook,
  facilities, mentoring plan, per-person senior-personnel documents, rate
  agreement), 30 questions with verified source pages, published as release
  assets with pinned hashes and a CI job that re-verifies every key citation
  against what the product actually extracts.

Their distribution model explains why this exists at all: the work is funded as
research-administration **capacity building** at institutions without R1
infrastructure. The deliverable is not a running service, it is a thing a peer
institution can install — so the office's judgement has to travel with the
code, as seeds.

## Why it is none of G1–G11

Because it is not a thing the platform can do. It is a thing somebody has to
know and then write down. A platform comparison asks "can it extract a field
set?" and ours now can; it does not ask "whose fourteen field sets, named by
what an office calls them, checked against which authority?"

The distinction is worth keeping sharp, because the two have opposite cost
curves. A primitive is bounded work with a definition of done — G1 took a spec,
a build and a measurement. Content is unbounded, it has no done, it cannot be
built from a guess about what a grants office wants, and **it is the only part
that cannot be bought back with engineering time later.** It accrues or it
doesn't.

## What we have, and what the vehicle is

We are not missing the mechanism. We are missing the material.

* **Field sets already live in skill reference files**
  (`structured-extraction.md` §4), resolved server-side, with the schema
  deliberately kept out of the turn's `toolConfig`. That is exactly the right
  vehicle: content in markdown, costing nothing until the skill activates.
* **The reduce step is a skill, not code** (§17.3), for the same reason stated
  explicitly in that spec — what an office wants out of a stack is
  institutional knowledge, it differs between offices, and it changes without
  a deploy.
* **Authoring tools exist** (`SKILL_AUTHORING_ENABLED`, the `my_skill_*`
  family, each write paused on an approval card), as do managed knowledge
  bases with Drive-folder sync, Shared Projects, and an RBAC catalog that can
  scope any of it to a role.
* **The content itself is one file:**
  `docs/examples/skills/funder-family/references/reduce-across-documents.md`
  — roughly a hundred lines, covering solicitation triage. Against 21
  workflows and 11 regulatory corpora.

So the honest statement of our position is not "we lack a capability they
have." It is: **we built the loading mechanism and have one cartridge.**

## What would close it, and what would not

**Would not:** generating a library of plausible field sets and skills from
the model's idea of research administration. It would read correctly and be
wrong in the places that matter — thresholds that moved, forms that were
retired, the categories one sponsor dropped last cycle. Their own corpus is a
caution here: an external policy review of v0.4.0 found it modelling five
requirements the sponsor had since retired, including a subaward threshold that
had doubled. Content authored without a practitioner is content that fails
exactly where a grants officer would notice first.

**Would:** one office, one lifecycle stage, authored with the people who do the
work, and measured. Concretely, in order:

1. **Pick the stage where we already have the primitive.** Pre-award
   solicitation triage — G1 is built, provenance is built, the reduce skill
   exists, and the measured envelope is known (reliable at ten documents,
   8/10 collision detection at twenty-five and fifty; `structured-extraction.md`
   §17.8).
2. **Author two field sets and one reduce skill with a grants office**, not for
   one. Their names, their thresholds, their authority documents.
3. **Point a managed KB at the authority** for that stage — one sponsor's
   policy guide, not eleven corpora.
4. **Measure it the way §17.8 measured the reduce**: planted facts, a stated
   interval, and a number we are willing to publish.

That sequence is also what tells us whether the other ten gaps matter. A
review queue (G7) and form output (G8) are speculative until an office is
using the thing daily and says what it is missing.

## What this does not settle

* **Reach still outranks content.** The closing verdict on G1 was that no part
  of it is usable without an API client. A library of field sets authored for
  an office that cannot reach them from the composer is content with the same
  problem the capability has. Authoring surface first, library second.
* **We have not asked an office.** Everything above is read off a public
  repository and our own specs. The seed library is strong evidence of what
  *that* institution's offices wanted; it is not evidence about ours.
* **Whether the content should ship in-repo at all** is undecided. Theirs is
  seed data because their unit of distribution is an installation. Ours is a
  deployment, and a skill can be authored and shared at runtime — which may be
  the better answer, and makes the RBAC and sharing story load-bearing rather
  than incidental.
* **None of this is an argument against G3, G4 or G6.** It is an argument
  against treating the eleven as the whole list.

## Sources

* Vandalizer's seed tree (`backend/seeds/` — 21 workflows, 11 knowledge bases,
  6 collections), its batch dispatch (`routers/workflows.py`,
  `workflow_service.run_workflow_batch`), and the `CSU-NSF-001` benchmark
  corpus with its CI validation jobs. Read 2026-10-20.
* `docs/specs/structured-extraction.md` — §4 (field sets as skill reference
  files), §15 (the retired "G5" label and the unattended-path trigger
  condition), §17 (N documents per call), §17.8 (ten-trial measurement).
* `docs/specs/source-provenance-verification.md` — G2 as shipped.
* **The G1–G11 list is not in this repository.**
  `source-provenance-verification.md` cites it as
  `_local-notes/vandalizer-parity-and-gap-analysis.md`, which is a dangling
  reference: that path is not tracked on `develop`, is not matched by any
  `.gitignore` rule, and does not exist in any checkout or worktree on the
  machine this was written on. The list survives only in the session transcript
  that produced it, which is why it is restated in full above — and why a
  shipped spec's "motivating gap" currently points at nothing a reader can
  open.
