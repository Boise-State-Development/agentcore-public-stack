# Reading a field set across a stack of solicitations

Reference material for a funder-family skill, loaded with `read_skill_file`
after `document_extract` has returned row sets for several documents.

This file exists because the reduce step is **a skill, not code**
(`docs/specs/structured-extraction.md` §17.3). A model can already reason over
fourteen row sets; what it cannot know is what a research-administration office
wants out of a stack. That is institutional knowledge, it differs between
offices, and it changes without a deploy — so it belongs in markdown that costs
nothing until the skill activates, not in a Python function that would freeze
one office's judgement into everyone's build.

## What the office is actually asking

Not "summarise these". The question behind a weekly intake sweep is always some
version of:

> Which of these should we put effort into, which can we drop, and what is
> going to bite us?

So lead with the answer to that. A list of fourteen summaries is the work
handed back undone.

## The shape of a useful reply

**1. The recommendation, first and in one line each.** Name the ones worth
pursuing and why, in the office's terms — fit, feasibility, deadline room.
Three sentences before any table.

**2. Dismissals with their reasons.** A solicitation ruled out is more useful
than one ignored, because the reason is reusable: *"cost share 50% — we have no
match"* tells the office something about the funder, not just about this call.
Never drop a document silently. If a document could not be extracted, say so
and say it is not in the comparison.

**3. Collisions and conflicts.** This is the part a per-document pipeline
cannot produce, and the main reason the whole stack is read in one turn:

- **Same deadline.** Two proposals due the same week is a staffing problem even
  when both are good.
- **Same people.** Two calls that both need the same PI, centre or facility.
- **Mutually exclusive.** Funders that forbid concurrent submissions, or limit
  one proposal per institution.
- **Internal competition.** Two departments likely to answer the same call.

**4. Outliers worth a second look.** A budget cap an order of magnitude from
its siblings, an indirect rate far below the negotiated one, a page limit that
implies a much larger proposal than the award justifies. An outlier is usually
either the best opportunity in the stack or an extraction error, and both are
worth naming.

**5. The comparison table, last.** The user already has one on screen and in
the spreadsheet, so repeat it only if a narrower one helps — the three
shortlisted against the two or three fields that decided it. Never re-type the
full table.

## Reading the per-value status, which is the part that gets skipped

Every value carries a status, and it changes what you may say about it:

| Status | What you may say |
|---|---|
| `supported` | State the value. It is in the passage cited for it. |
| `quote_unsupported` | **Do not state it as fact.** The passage is real and the value is not in it. Name the field and say it needs checking. |
| `unassessed` | State it, but not as confirmed — a yes/no or a choice from a list cannot be matched against prose. |
| `unverified` | No passage was found. Do not present it as coming from the document. |

Two habits that matter more across a set than they do on one document:

- **A null is an answer.** "The solicitation does not state a cost-share
  requirement" is useful, and guessing is worse than a blank because someone
  will act on it.
- **Never let a weak value carry a recommendation.** If the budget cap that
  made something your top pick is `quote_unsupported`, the recommendation is
  "check this first", not "pursue this". Say which value the recommendation
  rests on.

`supported` means *this value appears in this passage, at this position*. It
does not mean the passage assigns the value to that field, and it is not a
judgement that the value is correct. A whole column of `supported` is not a
clean bill of health.

## Habits worth keeping

- **One `document_extract` call for the whole stack.** Calling it per document
  throws away every comparison above, and costs far more.
- **Narrow with `pages` when the fields are all at the front.** It is the main
  lever on what a sweep costs, and provenance is still checked against the
  whole document.
- **Fourteen fields per set is the ceiling** Bedrock will compile. A wider set
  has to be split in two and run separately; say so rather than dropping
  columns.

## What not to do

- Do not re-type the tables or write a download link. Both are already on
  screen.
- Do not rank by a score you invented. Rank by the fields the office named, and
  say which field decided each call.
- Do not smooth over a failure. "Fourteen solicitations reviewed" when three
  could not be read is a judgement on eleven presented as a judgement on
  fourteen.
