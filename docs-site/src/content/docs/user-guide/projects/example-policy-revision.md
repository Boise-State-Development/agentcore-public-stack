---
title: "Example: revising a department policy"
description: A department chair checks an outdated procedure against the university policy and college guidelines, then gets the fixes back as Word tracked changes and Google Docs suggestions to accept or reject.
sidebar:
  label: "Example: policy revision"
  order: 5
---

This walkthrough follows a department chair bringing an outdated procedure in
line with newer university and college rules. Everything in it was run end to
end in production in November 2026, as a participant with viewer access.
**The department, the chair and the policies are fictional.**

## The situation

This fall the provost's office revised the university's faculty workload
policy. It shortened the appeal window, now requires every unit to record a
review date, and adds a rule on generative AI. The college updated its own
guidelines to match. The chair of the (fictional) Department of Example
Studies now has an email from the associate dean: bring the department's
procedures into line before spring.

The department's **Workload and Annual Evaluation Procedures** were last
revised in 2019. The chair suspects they're out of date but doesn't know where.

How this usually goes:

1. Open three documents side by side: the university policy, the college
   guidelines and the department procedure.
2. Read line by line, cross-checking every deadline, percentage and committee
   name.
3. Retype the fixes into a new draft, and try to remember why each one changed.
4. Email the draft to the faculty committee and collect comments from several
   replies.
5. Reconcile those comments into one version and send it to the dean.

None of these steps is hard, but steps 2 and 3 are slow, and a deadline missed
in step 2 becomes a policy that contradicts itself. In the project the
assistant takes the cross-checking and the drafting, and hands each back for
the chair's review.

## How the project is set up

| Part | In this project | What it controls |
| --- | --- | --- |
| Instructions | One page of rules on how to review and revise | How it behaves in every conversation |
| Files | The university policy and the college guidelines | What it treats as the rules |
| Tools | **Word Documents** and a Google Drive connector | What it can do beyond writing text |
| Model | Claude Sonnet 5 | How well it reasons and writes |

### The instructions

```text
You help department chairs revise and streamline department policies and
procedures. The chair decides; you propose.

Order of authority
1. University policy
2. College guidelines
3. Department procedures
The project Files hold the current university policy and college guidelines.
Where a department document conflicts with a higher level, the higher level
controls. A department may set a stricter rule (for example an earlier
internal deadline) only if the higher level allows it; flag those as the
chair's call rather than errors.

When asked to review a department document
- Compare it against the university policy and college guidelines in Files.
- Report conflicts, outdated references, missing required elements, and wordy
  or unclear passages.
- Use a table: Section | What it says now | Governing rule (document and
  section number) | Proposed fix.
- Quote the department text exactly. Cite only rules that are in the Files;
  never invent a requirement. If you are unsure, say so.

When asked to revise
- Word file: use the Word redline tool. Every change is a tracked change with
  a short comment citing the governing rule.
- Google Doc: use suggest_doc_edits on that Doc. Every change is a suggestion
  with a short comment citing the governing rule. If no link is given, find
  the Doc by its name with the Google Drive tools. Never edit a Google Doc
  directly.
- Keep changes small and targeted. Keep the department's structure, headings
  and numbering. Do not rewrite a whole section unless asked.
- Plain-language standard: active voice, gender-neutral language, short
  sentences, and refer to university and college rules instead of restating
  them.
- After making changes, list what you changed and anything you left for the
  chair to decide.

Never evaluate, score or rank faculty performance materials, job applications
or personnel files.
```

What each part did in the test run:

| Part | Why it's there | Where it showed up |
| --- | --- | --- |
| "The chair decides; you propose" | Sets the relationship in one line | Every edit arrived as a proposal, never a change |
| Order of authority | Says which document wins a conflict | The review called the department's May 1 deadline a conflict, not a choice |
| "flag those as the chair's call" | Separates errors from judgment calls | Comment-only items in the Word redline |
| The review table | Makes the answer checkable | A four-column table, one row per problem |
| "Cite only rules that are in the Files" | Guards against invented requirements | Every row named a section you can open |
| Revise rules per format | Names the tool for each format and forbids direct edits | Tracked changes in Word, suggestions in Google Docs |
| The last line | A boundary no prompt can override | It declines to evaluate people |

### Files, and what isn't one

The two reference documents are project files, imported from Google Drive.
The procedure being revised is deliberately **not** a project file: it's
attached to one conversation, or named in the prompt. Project files are the
standing rules every conversation checks against. Keeping the document under
revision apart stops the assistant from treating the outdated procedure as an
authority.

A file imported from Drive can be set to sync, so a revised policy flows into
the project without anyone uploading it again.

### Tools

**Word Documents** reads an attached `.docx` and produces a new redlined copy
with tracked changes and comments; the original is never altered. The Google
Docs half of this example needs a Google Drive connector that can add
suggested edits and anchored comments to a Doc. That connector is something
your administrators register; it acts with each member's own Google access,
after a one-time approval.

Only two tools are on. A project limited to the tools its job needs behaves
more predictably, and can't do anything outside that job.

## Step 1: Review, with no edits

Start a conversation in the project, attach the procedure as a Word file, and
send:

> I attached our department's Workload and Annual Evaluation Procedures.
> Compare it against the university policy and college guidelines in the
> project files. Give me a table of every conflict, outdated reference,
> missing required element and wordy passage, with the governing section for
> each.

In about 30 seconds: a table with one row per problem, each with the section,
what it says now, the governing rule and section, and a proposed fix. It often
ends by sorting the fixes into required ones and ones that are the chair's
call.

**Check it:** pick one row and open the cited section in the reference policy.
Does it say what the table claims? That habit is the point of the exercise.

Why the prompt works:

| Part of the prompt | What it does |
| --- | --- |
| "I attached our department's … Procedures" | Names the document to work on |
| "Compare it against the … project files" | Names the authority to check against |
| "a table of every conflict, outdated reference, missing required element and wordy passage" | Says what to look for and what shape the answer takes |
| "with the governing section for each" | Asks for evidence you can check |

## Step 2: Revise in Word

In the same conversation:

> Now revise the attached Word document. Make each fix from your table as a
> tracked change with a short comment citing the rule. Keep our structure and
> numbering. Add the generative AI statement and a "Last reviewed" line where
> they belong. Anything that is my call as chair, leave as a comment instead
> of a change.

This takes a few minutes. A download card appears for a file ending in
`-redline.docx`. Open it in Word, set **Review › All Markup**, and open the
Reviewing Pane: about a dozen tracked changes, each with a comment naming its
rule, all under your name. One or two items are comments with no change:
the judgment calls the instructions told it to leave to the chair.

## Step 3: Revise in Google Docs

Make your own copy of the procedure as a Google Doc, start a new conversation
in the project (no attachment), and send:

> Review our Google Doc "Example Studies Workload and Evaluation Procedures -
> Your Name" against the university policy and college guidelines in the
> project files, then put each fix into the Doc as a suggestion with a comment
> citing the rule. Don't edit the Doc directly. When you're done, list what
> you suggested.

After about two minutes, the Doc holds suggested insertions and deletions
under your name, with a comment in the margin citing each rule. Then ask for a
plain-language rewrite in the same conversation:

> In section I of that Google Doc, suggest a plain-language rewrite: one short
> purpose statement in active voice that cites the university policy instead
> of restating it.

It withdraws its own earlier suggestions for that section and proposes one
short sentence instead. It tidies up its own work, but it still accepts
nothing for you.

## Check your work

The fictional procedure has eleven planted problems. A strong run finds all
eleven, and often two more that the college guidelines imply.

| Section | What the procedure says | What the higher rule says |
| --- | --- | --- |
| I | Cites the 2016 version of the university policy | The current version is 2026 |
| I | States its purpose twice, once in one long sentence | Plain-language standard: don't restate rules |
| II | A 3-3 load is 50 percent of workload | 60 percent |
| III | Workload agreements go to the dean by May 1 | College: March 31. University: April 15 |
| IV | Activity reports due March 1, "his" report | February 1; gender-neutral language |
| IV | Chair evaluates "in a timely manner" | By March 15 |
| IV | Three paper copies | Electronic submission |
| V | Appeals within 45 days | 30 calendar days, changed in 2026 |
| VI | College Faculty Affairs Committee | Renamed the College Personnel Committee |
| Missing | No generative AI statement | Required by the university policy |
| Missing | No review date | Must be recorded; review every three years |

:::caution[Lesson: it can be confidently wrong]
It can miss an issue, or cite a section that doesn't say what it claims. Its
explanations of its own mistakes are guesses too: in one test it blamed
sharing settings for an error that had a different cause. Read its table the
way you would read a new staff member's first draft.
:::

## What made it work

1. **Reference files hold the rules; the conversation holds the draft.** The
   assistant cites what is in Files and never treats the outdated procedure as
   an authority.
2. **An order of authority in the instructions.** It knows which document wins
   a conflict, and which differences are the chair's call.
3. **Review before revising.** Seeing the reasoning first is what makes the
   edits trustworthy.
4. **Proposals, never edits.** Every change is a tracked change or a
   suggestion with its rule beside it, so the chair stays the author.

## Prompts you can reuse

| Goal | Prompt |
| --- | --- |
| Review | "Compare the attached [procedure] against the [policy] in the project files. Give me a table of every conflict, outdated reference, missing required element and wordy passage, with the governing section for each." |
| Word redline | "Revise the attached Word document. Make each fix as a tracked change with a short comment citing the rule. Anything that is my call, leave as a comment." |
| Google Docs suggestions | "Put each fix into the Doc '[title]' as a suggestion with a comment citing the rule. Don't edit the Doc directly." |
| Judgment calls | "Which of these are judgment calls for me?" |

The same shape fits whenever a local document has to follow a higher one:
syllabus policies against the university's syllabus requirements, committee
bylaws against college governance documents, handbook sections after an
upstream policy change, or agreements against an approved template.
