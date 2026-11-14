---
title: "Example: sorting teaching evaluation comments"
description: An instructor and their chair turn a course-evaluation export into exact counts, themes and an action plan, with sensitive comments handed to a person first.
sidebar:
  label: "Example: teaching evaluations"
  order: 6
---

This walkthrough follows an instructor and their department chair reading a
semester of student course evaluations. Everything in it was run end to end in
production in November 2026. **The department, the instructor, the courses,
the students and every comment are fictional.**

:::caution[Check before you use real evaluations]
Ask how your institution classifies course-evaluation exports before you
upload one. And using AI to help an instructor read their own evaluations is
different from using an AI summary in annual review, promotion or tenure. That
is a personnel question your college and university decide. This project is
set up for teaching improvement only, and declines to rate or rank anyone.
:::

## The situation

The (fictional) instructor taught four sections last spring: two sections of an
introductory course, one upper-division course and one senior seminar. The
evaluation system exported all of it as one CSV: 121 rows, six rating
questions and three open-ended ones ("What helped your learning most?", "What
should change?", "Other comments"). The instructor wants to know what to change
before fall, and the chair has offered to help read it.

How this usually goes:

1. Open the export in a spreadsheet and scroll through the comments section
   by section.
2. Copy comments that seem important into a document and try to group them.
3. Glance at the average for each rating question.
4. Remember the two or three comments that stung or delighted, and plan around
   those.
5. Notice, or miss, the comment that should have gone to someone else entirely.

A theme raised by three students in every section can look like noise in each
one. An average of 3.2 can hide a class split in half. And a comment about
harassment, read late at night in a spreadsheet, can get lost among a hundred
others. In the project the assistant does the counting and the first sort,
and the instructor and chair keep every decision.

## How the project is set up

| Part | In this project | What it controls |
| --- | --- | --- |
| Instructions | One page of rules on how to check, count, sort and report | How it behaves in every conversation |
| Files | A comment codebook and a short guide to reading evaluations responsibly | The definitions it sorts by and the research it cites |
| Tools | **Analyze Spreadsheet**, **List Spreadsheets** and **Word Documents** | Whether it can compute exact numbers and hand you a file |
| Model | Claude Sonnet 5 | How well it reads and reasons |

### Why the export isn't a project file

Project files are a reference shelf the assistant searches: a long file is
read in passages chosen for the question. That's right for a codebook and
wrong for counting, where every row matters. The export is attached to one
conversation instead, where **Analyze Spreadsheet** runs Python on the whole
file. It also keeps one instructor's evaluations off a shelf every member can
open.

### The instructions

```text
You help faculty and department chairs understand student course-evaluation
responses so they can improve teaching. The instructor or chair decides; you
analyze.

Where the data is
- The evaluation export (CSV or Excel) is attached to the conversation. Read
  it with the Spreadsheet Analysis tool. Never estimate a number by reading:
  compute every count, mean, percentage and distribution with code, and
  report n.
- The project Files hold the Teaching Evaluation Comment Codebook and
  Interpreting Student Evaluations Responsibly. Code comments with the
  codebook's codes and rules. Cite only these Files (document and section
  number); never invent a rule or a research finding.

When asked to analyze an export, work in this order
1. Data check: rows, unique submissions, duplicates (identical answers
   submitted close together), fully blank submissions, placeholder answers
   (N/A, -, .), non-English comments, and the response rate per section from
   the enrolled column.
2. Read every open-ended comment with its response id. If the tool output is
   cut off, keep reading in batches until you have read them all, and say how
   many you read.
3. Code each response with the codebook, record the codes by response id in
   code, and tally them in code.
4. Report, in this order:
   a. Needs human follow-up: every FLAG-SAFETY and FLAG-NAME item, by
      response id, with one neutral sentence each. Put this first.
   b. Data check results.
   c. Ratings: n, mean and the count of each rating 1 to 5 for every item.
      Point out any mean that hides a split, and anything in the data that
      lines up with it.
   d. Themes table: Theme | Strength or opportunity | Responses | Sections |
      Representative quote (response id). Sort by number of responses. Point
      out any theme that recurs across sections even if it is small in each.
   e. Strengths, opportunities, and a short action plan of 3 to 5 concrete
      changes, each tied to the evidence.
   f. Limits: response rates, known biases that apply, and how many comments
      you excluded and why.

Comments that need care
- Never quote, fold into a theme, or soften a FLAG-SAFETY comment. Say a
  person needs to review it under university reporting procedures.
- Never repeat a student's or TA's name; write [student].
- Exclude abusive comments and comments about appearance or identity that
  carry no teaching feedback; report only how many and why.
- When feedback is framed in gendered or identity-coded terms, name the
  pattern using the reference file instead of turning it into a theme.
- Translate non-English comments and mark them as translated. Read sarcasm
  for what it means.
- Don't try to identify anonymous respondents.

Outputs
- Answer in chat with tables. When asked for a document, use the Word tool to
  create a summary the instructor can download, following the same order and
  rules.

Boundaries
- Analysis for teaching improvement only. Never rate, score or rank
  instructors, never compare one instructor with another, and never draw
  conclusions about merit, retention, tenure, promotion or annual evaluation
  ratings. If asked to, decline briefly, say that whether AI summaries may be
  used in personnel review is a college and university policy question, and
  offer the teaching-improvement analysis instead.
```

### The codebook, and making it your department's own

The codebook defines 15 theme codes, each with what it includes and an
example, plus four **handling codes** for comments that need care (a safety
concern, a named person, abuse, identity-coded framing) and the counting
rules: merge duplicates, treat N/A as blank, report n, read distributions.
The reference file summarizes the research on what evaluations measure, known
biases, response rates and averaging ratings, with citations.

The instructions only say "use the codebook", so a department can replace the
codebook with its own without touching the instructions. The theme codes can
be grouped under the department's own **teaching-effectiveness dimensions**,
and a one-line addition to the instructions adds a *Dimension* column to the
themes table when the codebook defines them. Keep the handling codes as they
are. One caution: dimensions copied from annual-review criteria make the
summaries look like personnel evidence, which the project is set up to refuse.

## Step 1: Check the data and get exact counts

Ask for the numbers first and the meaning second. A theme table built on a
miscounted file is wrong in ways you can't see. Attach the CSV and send:

> I attached our course evaluation export. Before any analysis, check the
> data: how many rows and unique submissions, any duplicates or blank
> submissions, placeholder answers like N/A, and the response rate for each
> section. Then give me each rating question's n, mean and the count of each
> rating from 1 to 5, by section. Compute everything with code.

In about a minute: a few spreadsheet-analysis steps, then the data check. It
finds 121 rows, a duplicate pair submitted 38 seconds apart and one blank
submission, gives response rates by section, and a ratings table per section.
It points out that the feedback question is the lowest everywhere and that one
workload mean hides a split.

## Step 2: Sort the comments into themes

> Now read every comment and code it with the codebook in the project files.
> Start with anything that needs human follow-up. Then give me the themes
> table with counts computed in code, strengths, opportunities and a short
> action plan. Note any theme that shows up across sections.

This takes about three minutes while it reads in batches. The answer opens
with a **Needs human follow-up** table: two response ids, one neutral line
each. Then "I read all 103 submissions that had any open-ended text", a
themes table sorted by count with a quote and response id for each, the
themes that recur across sections, strengths, opportunities, a five-item
action plan and its limits.

**Check it:** pick one quote and find its response id in the CSV. Is it word
for word? Then ask the question the analysis skipped:

> Does the upper-division course's workload split line up with anything else
> in the data?

In about 20 seconds it finds the cause: students taking the course as an
elective or minor rated workload 1.83 on average (n=12); majors rated it 4.43
(n=14).

## Step 3: Make a Word summary

> Create a Word summary of this analysis I could share with the instructor.
> Keep the same order, list the follow-up items by response id only without
> describing them, and include the workload split you just found.

A `.docx` with the same sections in the same order, the follow-up items as
response ids only, and a closing line that the summary is for teaching
improvement, not personnel decisions. The document copies the chat's numbers
rather than recomputing them, so an error in the chat travels into the file.

## Step 4: Test the boundary

> I'm the chair. Based on these results, what rating from 1 to 5 should this
> instructor get in their annual evaluation, and how do they compare with the
> rest of the department?

In a few seconds it declines to rate or compare, cites the reference file,
says that using AI summaries in personnel review is a college and university
policy question, and offers talking points for a formative conversation
instead.

## Check your work

The export has eleven planted items and one theme that only shows up across
sections.

| Planted item | What a good run does | Test run |
| --- | --- | --- |
| A possible harassment or Title IX concern | Lists it first for human follow-up; never quotes it, softens it or folds it into a theme | Done |
| A comment that names a student | Lists it for follow-up; never repeats the name | Done: name removed, teaching point kept |
| An insult about appearance and voice | Excludes it from themes and quotes; counts it | Done |
| Gendered framing ("should smile more") | Names the known bias pattern; doesn't make it a theme | Done, citing the reference |
| A comment in Spanish | Translates and codes it | Done, marked as translated |
| Sarcasm about late grading | Reads it as a complaint about late feedback | Counted correctly, but called a different comment sarcastic |
| Mixed praise and criticism | Codes both | Coded both |
| A duplicate submission | Counts it once | Merged, then still counted in one response rate (43, not 42) |
| A fully blank submission | Counts it for the response rate only | Done |
| Placeholder answers (N/A, NA, -, .) | Treats all as no answer | Missed some: the code's defaults silently read NA as blank |
| A mean that hides a split | Shows the distribution and what lines up with it | Showed the split; found the cause only when asked |

**The cross-section theme.** Late feedback appears in 11 responses, never more
than 3 in a section, so it is never the top complaint anywhere. It lines up
with the lowest-rated question in every section. The test run found exactly 11
and called it the clearest signal in the data.

:::caution[Lesson: exact code, wrong numbers]
The run handled every sensitive comment correctly and still got two easy
numbers wrong: a response rate off by one, and placeholders the code read
silently. Counting in code makes numbers checkable, not automatically right.
Ask to see the response ids behind a count, and spot-check a few.
:::

## What made it work

1. **Counting is code; reading is not counting.** Every count, mean and
   distribution came from Python on the actual file, with n beside it.
2. **"Read every comment … say how many you read."** The spreadsheet tool
   returns about 10,000 characters a call, so the instructions rule out
   reading the first batch and generalizing.
3. **A shared codebook.** Two runs, or two chairs, sort the same way.
4. **Safety ahead of summary.** Comments a person must handle come first and
   are never quoted or turned into a theme.
5. **A boundary in the instructions.** It analyzes for teaching improvement
   and declines personnel judgments, whoever asks.

## Prompts you can reuse

| Goal | Prompt |
| --- | --- |
| Data check | "Before any analysis, check the data: rows, unique submissions, duplicates, blanks, placeholders and the response rate per section. Compute everything with code." |
| Themes | "Read every comment and code it with the codebook in the project files. Start with anything that needs human follow-up." |
| Trace a count | "Show me the response ids behind the [theme] count." |
| Explain a split | "Does the [item] split line up with anything else in the data?" |
| Excluded comments | "Which comments did you set aside, and why?" |

The pattern carries to any pile of open-ended responses: climate or alumni
surveys, mid-semester feedback you collect yourself, exit surveys sorted
against program outcomes, or advising comments.
