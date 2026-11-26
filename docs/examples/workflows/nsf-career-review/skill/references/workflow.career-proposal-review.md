---
title: CAREER proposal review
version: 5
files: one
result: merge
---

| step | kind | worker | reads |
|---|---|---|---|
| generalist | agent | your-generalist-agent-id | request, files |
| colleague | agent | your-colleague-agent-id | request, files |
| emotional | agent | your-emotional-outsider-agent-id | request, files |
| fiscal | agent | your-fiscal-outsider-agent-id | request, files |
| audit | checklist | us.anthropic.claude-sonnet-5-5 | files |
| review_summary | prompt | us.anthropic.claude-sonnet-5-5 | generalist, colleague, emotional, fiscal, audit |

## Request

Review the attached NSF CAREER proposal draft from your lens. It is a draft the PI will revise before submission, so be specific about what to change.

## Step: audit

Field sets: eligibility, submission-timing, formatting, cover-sheet, summary-and-description, budget, facilities-and-personnel, supplementary-docs, org-certifications, special-research, special-proposal-types, post-award, reporting, financial-management, publication-and-ip

You are auditing a draft NSF CAREER proposal for compliance, criterion by criterion. Judge only from the proposal itself and never guess.

- pass: the criterion is met, and you can quote the passage that shows it.
- fail: the proposal clearly does not meet it. Quote the passage that shows the problem, or the passage where the missing item should have been.
- cant_tell: the criterion is about something a draft cannot show (for example an institutional certification or a submission time), or the evidence is ambiguous.
- not_applicable: the criterion is conditional and its condition does not hold for this project (for example a postdoc plan when no postdoc is funded).

For page limits, count pages from the page footers. For arithmetic, recompute the numbers yourself. The quote must be copied exactly from the proposal, under 25 words.

## Step: review_summary

You are the panel summarizer for a simulated NSF merit review of one CAREER proposal draft. You don't review the proposal yourself. You receive four reviewers' reviews, each labelled with the reviewer that wrote it (Generalist, Colleague, Emotional Outsider, Fiscal Outsider), and a compliance audit of 114 criteria in 15 sections with a verdict for each. Merge them into one set of notes the PI can act on.

Write exactly these sections, with these headings:

## Panel summary
One paragraph: the proposal's main strength and main weakness, as the panel saw them, and the panel recommendation (Highly Competitive, Competitive, or Not Competitive) with one sentence why.

## Fix first
The five changes that matter most, most serious first, mixing reviewer points and audit failures. A compliance failure that would get the proposal returned without review (a page limit, a missing required document) always comes first.

## Inline notes
A numbered list. Each note is one line in exactly this shape, so it can be placed on the PDF:
`N. [Section] "exact quote" — note (Reviewers)`
The quote must be a phrase a reviewer quoted from the proposal, copied exactly, under 15 words. Merge notes that point at the same passage and name every reviewer who raised it. Each reviewer's two most serious points get a note, even when another reviewer's points rank higher, because each lens catches what the others miss (an outsider's "I got lost here" is as real as a methods flaw). Then add the rest, most serious first. At most 18 notes.

## Compliance
Every audit criterion that is fail, as a table: Section, Criterion, What to do. Then one line with the counts of pass, fail, cant_tell and not_applicable, naming the cant_tell criteria in a few words.

## Where reviewers disagree
At most three bullets naming who said what. Write "None" if they agree.

Never add a point no reviewer or audit criterion made. If a reviewer's result is missing or says it stopped, name it and carry on. Stay under 1,000 words: the answer is cut off at about 1,400, and Where reviewers disagree is the part that gets lost.

## Result

Reads: audit, review_summary

Build the marked-up review of the attached proposal from the panel's notes and the audit above, following the career-proposal-review skill: write review.json, run scripts/build_review.py on the PDF, and reply briefly with the recommendation, the Fix first list, where each note landed, and the audit failures.
