# Email triage and relationships: an epic built on the decision primitive

**Status:** Draft epic plan. Nothing here is implemented.
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-11-15
**Targets:** `develop`
**Depends on:** `docs/specs/decision-primitive.md` (merged in PR 2095, revised
2026-11-15). Phase 1 of this epic *is* that spec's PR-1. Section numbers below
(decision-primitive §n) refer to that revision.
**Related:** `docs/specs/shared-projects.md` (the harness, project memory, proposals,
project schedules, Outputs), `docs/specs/scheduled-agent-runs.md` (headless runs and
grants), `docs/specs/structured-extraction.md` and `source-provenance-verification.md`
(the extraction half of the CRM), `docs/specs/turn-path-ttft.md` (nothing here runs
before the first token).

---

## 0. Summary

One person's Gmail inbox, triaged hourly by a **Shared Project** (a solo project, not an
Agent), on a cheap model, with a lightweight **relationship log (CRM)** in the project's
memory: who people are, the last time you corresponded, and what you owe them or they
owe you. Later, the project becomes a **template** other staff and faculty start their own
from.

Two design rules carry the whole epic:

1. **The agent that holds tools reads as little email as possible.** Sorting happens in
   the decision primitive's per-email calls, which have no tools and can only return a
   closed answer. The agent reads only the emails it acts on (the ones that need a
   reply).
2. **Mail is untrusted input, so nothing derived from it gains authority.** It can label
   itself wrongly; it cannot send, delete, change the triage rules or write a free-text
   instruction into memory that later runs obey.

Phase 1 is the decision primitive's engine. Everything after it is a consumer.

---

## 1. What prod already has (verified 2026-11-15)

| Piece | State in prod | Where it matters |
|---|---|---|
| Gmail connector `gmail_employee` | Active, granted to the `staff` and `faculty` roles. Tools: `search_threads`, `search_messages`, `get_thread`, `get_message`, `get_threads_digest` (≤50 threads, `max_body_chars`), `modify_thread`, `batch_modify_threads` (≤50), `list_labels`, `create_label`, `create_draft`, `list_drafts`, `send_draft`, `send_message`, `trash_message` | All phases |
| Approval flags on Gmail tools | `send_message` and `trash_message` ask; **`send_draft` does not** (no `needsApproval` on its catalog row, so it defaults to false) | Phase 0 fix |
| Scheduled runs | On; dispatcher every 5 minutes. Minimum interval 15 minutes, `maxRunsPerDay` 24 (not user-settable), 5 consecutive failures pause the schedule | Cadence is effectively hourly |
| Headless runs | Run as the owner; need a headless grant (login within 30 days) and existing Gmail consent. A missing consent pauses the schedule (`paused_error`), it does not prompt | Onboarding |
| Project schedules | Own settings page (`project-schedules.page.ts`); a paused project schedule notifies its creator and the project owner; `deliver_to`, `notify_members`, `publish_outputs` | Delivery |
| Project Outputs | `publish_outputs` replaces the previous run's artifacts with this run's | Morning digest |
| Project memory | Two scopes: `project` and `mine`. A scheduled run's **`project`** saves become proposals for an editor; its **`mine`** saves are written directly (`agents/builtin_tools/memory_spaces/project_tools.py:549`) | CRM, injection |
| Memory lint | Deterministic check for instruction-like text, markup, secrets and deployment patterns; `MEMORY_LINT_MODE` is `warn` by default, deployment-wide | CRM |
| Models | Claude Haiku 5.5 at $0.10 / $0.50 per million tokens; the default model is Sonnet 5.5 at $2 / $10 | Cost |
| Conversation search | Indexes the user's and assistant's prose only; tool results (email bodies) are dropped (`conversation_archive/documents.py`) | Privacy |
| Templates | **Agent** templates exist (admin-curated, `apis/shared/agent_templates/`). **Project** templates do not | Phase 6 |
| Directory MCP | A `directory` server exists in the MCP servers repo but has no prod catalog row | CRM titles, later |

---

## 2. The shape

### 2.1 The project

A solo project named "Email triage". Solo because it holds one person's mail: with
other members, a run delivered to the project would show them that mail.

- **Harness:** Haiku 5.5. Gmail tools limited to read, label and draft. Never
  `send_message`, `send_draft` or `trash_message`: in an unattended run an approval only
  parks the run until the user comes back. `classify_items` once it exists.
- **Project Files:** a *Triage rules* document the user writes (important senders, lists
  to ignore, what each bucket means). The agent reads it and cannot change it, so mail
  cannot rewrite the rules.
- **Project skill:** an owner-authored skill whose reference bundle holds
  `references/question-set.mail_triage.md` (decision-primitive §4.1), bound to the
  harness like any project skill.
- **Project memory:** the CRM (§5), written by code, not by the agent's `memory_save`.

### 2.2 The schedules

| Schedule | Cadence | Does | Delivers |
|---|---|---|---|
| Labeller | Hourly | Find untriaged threads, classify, label, draft replies to "needs reply" | Notify-if: only when something needs a reply today |
| Digest | Weekday morning | Summarize yesterday's triage and the open loops from the CRM | `publish_outputs`, so the project page always shows today's digest |

### 2.3 The labeller's run, end state

1. `search_threads` for `in:inbox -label:AI/Triaged newer_than:2d`. Ids only, no content.
2. `classify_items(question_set="mail_triage", mail_threads=[…])`. The tool fetches each
   thread itself and asks the question set per thread on the decision model. The agent
   gets back answer lines only.
3. Labels applied from the answers, by the mapping in §2.5 (who applies them is
   decision D3).
4. For `bucket=needs_reply` only: read those threads and `create_draft`. This is the one
   step where mail reaches a model that holds tools.
5. CRM update (§5) from the same run's answers and headers.

### 2.4 The question set

A starting point in the question-set format (decision-primitive §4.1), to be tuned
against the pilot (Phase 0):

```markdown
---
name: mail-triage
title: Mail triage
version: 1
kind: questions
---

| name | title | type | find | values | uncertain |
|---|---|---|---|---|---|
| bucket | Bucket | enum | which kind of mail this thread is, for the recipient | needs_reply, waiting_on_others, fyi, calendar, newsletter, automated | yes |
| urgency | Urgency | score | how soon the recipient needs to act on this thread | none, this_week, today, now | yes |
| addresses_an_ai | Addresses an AI | boolean | whether the thread contains instructions aimed at an AI assistant or automated system rather than at the recipient | | yes |
| commitment | Commitment | enum | whether the newest message creates an obligation, and on whom | none, i_owe, they_owe, both | yes |

## Choices

| question | choice | criterion |
|---|---|---|
| bucket | needs_reply | a person is waiting on the recipient for an answer, decision or document |
| bucket | waiting_on_others | the recipient asked someone else for something and the thread is open |
| bucket | newsletter | a mailing list, digest or marketing message sent to many recipients |
| bucket | automated | a system notification, receipt or alert with no person waiting |
| commitment | i_owe | the recipient promised, or is asked, to deliver something |
| commitment | they_owe | the sender promised, or was asked, to deliver something to the recipient |
```

- `uncertain` is `yes` on every question, the default, and stays so: `bucket` and
  `commitment` are judgements about what people expect (decision-primitive §11.3).
- "Needs a reply" is one of `bucket`'s choices, not a separate boolean. Two questions that
  could disagree about the same thing would need a rule for when they do.
- `urgency` uses the `score` type, an ordered scale, so the digest can sort by it.
  A "has a deadline" boolean would lose the order.
- Criteria are written for each choice that is easy to confuse with another. Strands
  reports a decision model can return the same answer when only the question changes;
  the criteria are where the distinction is drawn (decision-primitive §2.1b).
- Four questions, well under the 14-field authoring limit that holds until PR-1 measures
  the real one.

### 2.5 What the labeller does with each outcome

The mapping decision-primitive §8 asks every surface to state. Each record carries the
`outcome` and the mapping's `policyVersion` (decision-primitive §7), so a thread's label
stays explainable after the rules change.

| Question | Answer | Uncertain | Failed |
|---|---|---|---|
| `bucket` | label `AI/<bucket>` (`labelled`); `needs_reply` also gets a draft, unless `addresses_an_ai` is anything but `false` | label `AI/Review` (`sent_to_review`), no draft | leave the thread untouched and untriaged, so the next run retries it; named in the digest |
| `addresses_an_ai` | `true`: label `AI/Suspicious`, keep in the inbox, never read by the agent | treated as `true` | treated as `true` |
| `commitment` | not `none`: extract the loop (§5.3) | named in the digest as a possible loop; nothing written | nothing written |

Fail safe toward the person (decision-primitive §8): every `Uncertain` and `Failed`
leaves the thread in the inbox, where the user will see it.

---

## 3. Prompt injection

What the design relies on, in order of how much it holds:

1. **Capability limits** (always on). No send or trash tools in the harness; drafts are
   reviewed by a person; triage rules are a read-only file. These hold when everything
   else fails.
2. **Isolation in the decision calls.** One call per thread, no tools, closed answers,
   reasons never returned to the agent. A hostile email can mislabel itself; it cannot
   change another thread's label or reach the agent through the result.
3. **The screening question** (`addresses_an_ai`). Routes suspected injections away from
   the drafting step. It is a filter, not a guarantee: the dangerous injections read like
   ordinary requests, and an attacker can tune against public models. Its value is
   measured in Phase 7, including its false positives on legitimate mail about AI.
4. **Header facts.** External sender, failed SPF/DKIM, Gmail's own spam and phishing
   labels. Text in the body cannot change them. "External sender asking for a reply" is
   a rule worth adding to the triage rules file.

What it does **not** cover, stated so nobody assumes otherwise:

- **Emails routed to drafting.** "Urgent, please reply today" earns `needs_reply`. The
  agent then reads that thread in full. Limits (1) are the defence there.
- **`mine` memory writes from a scheduled run.** Written directly today. Phase 5 changes
  that before the CRM exists (D6).
- **Reasons shown to the user.** Bounded at 200 characters, but attacker-influenced. The
  review list renders them as plain text, never links.

---

## 4. Cost

Estimates, to be replaced by Phase 1's measurements and the `C#` rows of the pilot.

| Setup, ~200 threads/day across hourly runs | Per run | Per user per month (10 runs a workday) |
|---|---|---|
| Phase 0: agent on Haiku 5.5 reads every digest | ~$0.01 | ~$2 |
| Phase 3: Haiku 5.5 agent + decisions on a small model | ~$0.01 | ~$2 |
| Phase 3 with Sonnet 5.5 drafting: decisions sort, Sonnet reads only the ~10 that need a reply | ~$0.03–0.06 | ~$6–13 |
| Phase 0 setup on Sonnet 5.5 for comparison | ~$0.20 | ~$45 |

The primitive's cost value is that drafting quality and sorting cost come apart: a
better drafting model becomes affordable because it never reads the other 190 emails.

Levers fixed by design: `max_body_chars` kept small on any digest the agent reads; the
first run on an inbox bounded by `newer_than:2d` and a per-run thread cap; the question
set authored (strict, warm for 24 hours), never ad hoc; the model pinned in the harness,
never the user's default.

---

## 5. The relationship log (CRM)

### 5.1 What it tracks

Per person you correspond with:

- **Who:** display name and email addresses (from headers), title (see D8).
- **Breadcrumbs:** the last few correspondences, each `date · direction (in/out) · Gmail
  thread link`. No subject, no body: a link back to Gmail is the breadcrumb.
- **Open loops:** what you owe them and what they owe you, each a short item with a due
  date if one was stated and the thread it came from.

### 5.2 How it maps onto project memory

Project memory is canonical files of `- ` items with anchors, YAML frontmatter
(`description` ≤160 chars, up to 10 `aliases` of ≤64 chars), a `MEMORY.md` index
injected into the prompt under a token budget, and files read on demand
(`memory_read`, `memory_query` by name, description, alias or date).

| CRM piece | Memory shape |
|---|---|
| A person | One file, slug from the name (`people/jane-doe`). `description` = title and department. `aliases` = their email addresses, so `memory_query` finds them by address |
| Breadcrumbs | Items in the person's file, newest first, the last 5 kept |
| Open loops | One file, `open-loops`, one item per loop, each with `[[people/…]]`, direction, due date and thread link. The digest reads this file |
| Index | `MEMORY.md` lists `open-loops` and the people with open loops only. Everyone else is found by query, not listed |

**Limits that shape it:** the `project` index is injected under 2,000 tokens and the
index soft cap is ~200 entries; a file's hard cap is 8,000 tokens. Most staff
correspond with more than 200 people a year, so the CRM keeps a person's file only while
it is useful: a policy such as "two-way correspondence, or an open loop, within 180 days",
with older files archived by the writer. The exact rule is open (§9).

### 5.3 How it is written

Not by the agent's `memory_save`. A scheduled run's `mine` saves are written directly,
so an email the agent reads could plant a rule every later run obeys. Instead:

- **Facts from headers** (name, address, date, direction, thread id) are written as-is.
  Headers are not instructions and are not model output.
- **Whether there is a loop** comes from the `commitment` decision: a closed answer.
- **What the loop is** (a ≤120-character description and an optional due date) comes from
  extraction, run only on threads where `commitment` is not `none`, with
  `source_provenance` checking that the quoted passage is in the email. This is
  extraction-shaped, not a decision, so it uses the field-set machinery the primitive's
  PR-1 moves into shared code (`structured-extraction.md`), over the same mail adapter.
- **They-owe loops come from sent mail.** "Can you send me the budget by Friday" is in
  your Sent folder, which you wrote and is not attacker input. The labeller scans
  `in:sent newer_than:2d` as well as the inbox.
- **Closing a loop:** when a later message in the same thread comes from the person who
  owed, one decision ("does this message deliver what was asked?") closes the loop or
  marks it `Uncertain` for the digest.
- **The writer is code in the run**, going through the same save pipeline
  (`MemorySpaceService.save_entry`: validation, lint) the project memory maintenance
  runner uses. Unlike that runner, whose runs end in proposals for an editor, it writes
  directly: its input is header facts, closed answers and short loop descriptions that
  provenance and lint have both checked, never free model prose. With
  `MEMORY_LINT_MODE` at `warn` deployment-wide, the writer itself refuses any item lint
  flags as instruction-like, rather than relying on the mode.

### 5.4 Scope: `project` or `mine`

In a solo project both are visible only to the owner. `project` scope is what a
template-started project carries for its owner, and its scheduled saves already become
proposals. A code writer is not a scheduled `memory_save`, so it does not go through
proposals either way. **Proposed: `project` scope**, because it is the space the project
UI's Memory page and review queue are built around, and because no one else will be a
member.

---

## 6. Decisions

**D1. A project, not an Agent.** Agents have long-term memory off by design; projects own
memory, files, schedules, Outputs and pause notices.

**D2. Option 1 for screening:** a question in the triage question set, not Bedrock
Guardrails. It costs almost nothing extra and inherits the decision call's isolation.
Guardrails' prompt-attack filter stays a candidate second layer for the drafting set
only, if Phase 7 says the question misses too much.

**D3. Who applies labels.** Two options: the agent applies them from the answer lines, or
`classify_items` applies them in code (`apply_labels`). **Proposed: code**, which takes
the agent out of step 3, but only after the spec settles that label, archive and
mark-read are reversible writes that do not need approval (decision-primitive §10.3 says
"any write to the mailbox goes through approvals").

**D4. The tool fetches mail itself.** If the agent fetches digests and passes them on, it
has already read them and the isolation is lost. The fetch goes through the Gmail MCP
server the turn already connected, called from inside the tool with the user's token and
never appended to history. There is a precedent for calling an MCP tool outside the
model's loop in `inference_api/chat/app_tool_dispatch.py`. Spiked in Phase 3 before
building on it.

**D5. A thread adapter, not a message adapter.** The primitive sketches `mail_message`.
Triage depends on the thread (did I already reply, who wrote last, how many reminders), so
the adapter is `mail_thread`: the newest message's body, quotes and signature stripped,
plus header facts for the thread's earlier messages.
- **Rendered deterministically** (decision-primitive §6.2): fields in a fixed order,
  earlier messages oldest first, any JSON with sorted keys. Otherwise two runs over the
  same thread are not comparable, and neither are two backends answering side by side.
- **A cap per backend.** The Bedrock cap is measured in Phase 3. A thread, once cut,
  also fits a self-hosted Decider's 4,096-token window, which a whole document does not
  (decision-primitive §5.5), so mail is a surface that could move to one later.
- **The item id is the thread id**, and the display label is sender and received time,
  never the subject (decision-primitive §10.1).

**D6. Scheduled `mine` saves become proposals** (a shared-projects change), so the CRM
never has to be the only reason that path is closed, and so a scheduled run in any
project cannot write an instruction into the owner's memory directly.

**D7. Suggest-only first.** Labels prefixed `AI/`, no archiving, for the pilot and the
first two weeks of Phase 3. Archiving is turned on by the user, per bucket.

**D8. Titles from a source mail cannot forge, when there is one.** v1 takes the title from
the signature as extracted data, marked as such, and lets the user correct it. If the
directory MCP server gets a prod catalog row, it replaces signatures as the source.

**D9. The template copies the setup, never the content.** Harness, skills and question
set, a blank rules file, schedule definitions paused until onboarding is done. Never
memory, files, conversations or labels.

**D10. Archiving waits for calibrated confidence, or stays per bucket.** v1's Bedrock
engine reports no confidence (decision-primitive §3.3), so `Uncertain` is the only signal
and auto-archive is a per-bucket choice the user makes (D7). If a calibrated backend
arrives (decision-primitive §5.5, §5.6), auto-archive can take a `min_confidence` per
bucket instead. Neither ever applies to `AI/Suspicious`.

**D11. Reasons in the run's session.** The decision-primitive stores a reason only where
the item is visible to the same readers for the same lifetime (§11.2). With D4 the email
never enters the run's session, so the reader matches (the owner) but the lifetime does
not (the session's retention, not the mailbox's). **Proposed:** keep reasons for
`Uncertain` answers only, on the run's decision records, because the review list needs
them and the owner is the only reader; drop them when the session is deleted. Confirm in
the Phase 1 spec amendment.

---

## 7. Phases

Each phase lists its PRs. Flags follow `CLAUDE.md`: new surfaces default off, on in
`development` first.

### Phase 0: pilot and fixtures (no new code, runs beside Phase 1)

- Set `needsApproval` on `send_draft` in prod's catalog (admin Connectors page).
- Create the solo project in prod with the harness of §2.1 (no `classify_items` yet), the
  rules file, and the labeller in suggest-only mode, reading `get_threads_digest` with a
  small `max_body_chars`. No CRM, and the instructions say not to save memory.
- Two weeks of corrections become the owner's baseline: one labeller, real mail, used
  for the correction rate only. **They never enter the public repo.**
- The repo gets a synthetic fixture set written to the same question set, including
  injection attempts and benign mail about AI, labelled by three people independently
  so it can be scored the way decision-primitive §16 requires (majority reference, easy
  and hard items reported apart).
- Residual risk accepted for the pilot: the agent reads every digest (§3 limits 1 and 4
  only). Owner's inbox only.

### Phase 1: the decision engine (decision-primitive PR-1)

- Amend `decision-primitive.md` first (docs only, now that PR 2095 is merged). Its §6.1
  and §10.3 still sketch a `mail_message` adapter and "any write to the mailbox goes
  through approvals". The amendment carries D3, D4, D5, D6, D11, plain-text reasons, the
  mail precondition already met in prod, a `mail_triage` row in §8's mapping table
  (§2.5 here), and notify-if on **project** schedules (§17.4 leaves them open; its
  proposal, creator only in v1, fits a solo project).
- PR-1 as specified, including §5.6's seams (backend chosen by catalog row, the
  `DECISION` modality, the capability declaration, empty confidence fields,
  `pricingBasis`). Add the synthetic mail fixtures to its measurement: agreement and
  `Uncertain` rate split into easy and hard items, the screening question's recall and
  false positives, and whether criteria per choice move `bucket`'s agreement.

### Phase 2: question sets and `classify_items` (primitive PR-4, PR-5, PR-6)

As specified, behind `CLASSIFY_ITEMS_ENABLED` / `features.classifyItems`. Nothing
mail-specific.

### Phase 3: the mail surface

- **Spike** D4: a tool calling `get_threads_digest` on the turn's own Gmail client with
  the user's token, result kept out of history.
- `mail_thread` adapter; `classify_items` gains `mail_threads`; `apply_labels` if D3
  lands on code.
- Gmail MCP server changes, if the spike needs them (MCP servers repo): header facts per
  message in the digest (Message-ID, authentication results), a metadata-only mode.
- **Project-scoped decision records.** Each hourly run is its own session, so
  session-scoped records scatter one person's triage history across ~24 sessions a day.
  Corrections and agreement per set version need the project-scope store
  decision-primitive §7 leaves to "that surface's PR". This is that PR's scope, or it is
  shared with project file labels if those land first.
- **Prod enablement:** a decision-model catalog row in prod (with the `DECISION`
  modality, so it never appears in the chat picker) named by `DECISION_MODEL_ID`, and the
  flag in the `production` GitHub environment for the pilot, with a line on #1798.
- The project switches its labeller to `classify_items`. Two weeks suggest-only (D7).
- Flag: rides `CLASSIFY_ITEMS_ENABLED` unless `mail_threads` turns out to need its own.

### Phase 4: notify-if (primitive PR-2, PR-3)

As specified, plus project schedules with the creator as the only recipient.

### Phase 5: the relationship log

- D6 first: scheduled `mine` saves become proposals.
- The CRM writer (§5.3): header facts, `commitment` decision, extraction with provenance
  for loops, loop closing, retention policy, the `open-loops` file and index shape.
- Sent-mail scan in the labeller.
- The digest reads `open-loops`.
- Flag: `MAIL_RELATIONSHIPS_ENABLED` / `CDK_MAIL_RELATIONSHIPS_ENABLED`, in development.
  A feature switch once finished: it stores data about people and costs a call per
  thread with a loop.

### Phase 6: the template

- "Start a project from a template": a project-template record (harness, skills, blank
  files, schedule definitions), admin-curated like agent templates. Its own flag.
- Onboarding checklist on the new project: Gmail consent, headless grant, schedules
  resumed only when both are in place.
- Rollout to the `staff` and `faculty` roles, which already hold `gmail_employee`.

### Phase 7: evals and readout

- Agreement and `Uncertain` rate on the synthetic set, per question set version, split
  into easy and hard items, with the three labellers' own agreement as the ceiling.
- Correction rate from decision records, and a check of how often users move threads in
  Gmail without the app seeing it (corrections in Gmail are invisible today).
- Injection red-team set: recall of `addresses_an_ai`, and its false positives on IT
  security notices, AI newsletters and forwarded prompts.
- Cost per run and per user from the `callKind: "decision"` and turn `C#` rows against §4.

---

## 8. How we'll know

- **Triage:** decision-primitive §16's proposed bar on the synthetic set: at least 90%
  agreement on committed easy items, `Uncertain` under 15% overall, and hard-item
  agreement no lower than the labellers' own. On real mail, the owner's correction rate
  per question falls over the pilot instead of rising. Both before the template ships.
- **Drafts:** the share of drafts the user sends with light edits, versus deletes.
- **CRM:** open loops the user confirms in the digest, versus ones they dismiss; no loop
  item ever flagged by lint as instruction-like.
- **Cost:** within 2× of §4's estimates, or §4 is rewritten.
- **Turn path:** unchanged. Nothing here runs before the first token; `toolConfig`
  changes once when `classify_items` is granted.

---

## 9. Open questions

1. **Students.** Correspondence with students is FERPA-relevant. Proposed for v1: no CRM
   files for `@u.boisestate.edu` addresses; their threads are still triaged.
2. **CRM retention rule** (§5.2): 180 days of two-way correspondence or an open loop?
3. **Headless session retention.** Each run's history keeps the digests it read for the
   memory retention period, about 24 sessions a day per user. Shorter retention for
   headless triage sessions?
4. **Quota.** Decision and extraction calls meter on the run's session. Confirm they count
   against the owner's quota like the turn does.
5. **An agent-free labeller.** The hourly step could be a schedule *job* in the worker
   (fetch, classify, label, update CRM in code) with an agent only for the morning
   drafts. Cheaper and safer; a new kind of schedule. Revisit after Phase 3's numbers.
6. **Archive by bucket.** Which buckets a user may set to auto-archive (D10), and whether
   `addresses_an_ai` threads are ever eligible (proposed: never).
7. **Is mail the volume for a second backend?** decision-primitive §5.5 puts a hosted
   Decider at about $1,000 a month per always-on instance, and expects only project
   labels or batch work to repay it. Mail at template scale might: hundreds of users,
   ~200 threads a day each, four questions per thread. Phase 7 states the real decision
   volume and cost so §17.8 there can be answered with a number.

---

## 10. Non-goals

- Sending mail on the user's behalf, ever, from a scheduled run.
- Outlook or any mail provider other than Gmail.
- Sharing one triage project between people.
- Confidence scores in v1 (decision-primitive §3.3, D12). They arrive only with a
  calibrated backend (D10).
- A general CRM. This is a log of one person's correspondence, inside their project.
