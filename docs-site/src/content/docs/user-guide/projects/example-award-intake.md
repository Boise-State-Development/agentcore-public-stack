---
title: "Example: setting up awards from their documents"
description: A research office runs one field list over five award documents at once, gets a setup table with a source check on every value, and learns which values still need a person.
sidebar:
  label: "Example: award intake"
  order: 7
---

This walkthrough follows a university research office setting up new awards
and checking ones it already has. Everything in it was run end to end on a test
deployment and again in production, in November 2026. **The awards, sponsors,
people and numbers are fictional.**

:::note[Two features this example needs]
The setup table comes from **structured extraction** and the labels on each
value from **source provenance verification**. Both are opt-in for each
deployment and off by default. If the assistant never shows a table under its
answer, ask your administrator whether they are on. See
[Feature flags](/agentcore-public-stack/configuration/feature-flags/).
:::

## The situation

Setting up an award means reading every document and retyping about 14 fields
into the system of record. Three things make that slow and easy to get wrong:

- **Look-alike values sit side by side.** A notice prints the budget period
  beside the project period. A subaward prints the prime award's period above
  its own. An obligated amount sits next to the total.
- **The latest document governs.** An amendment changes the end date and the
  money, so the original notice is no longer right.
- **Some answers are blank, or have to be worked out.** A document may say
  nothing about cost share, or start "on the date of the last signature".

A wrong pick looks just like a right one until a report is late or a charge is
questioned. A plain AI chat reads faster than a person, but it answers every
question with the same confidence, so you can't tell which answers to check.

The office has five documents to work through:

| # | Document | What it is |
| --- | --- | --- |
| 01 | Notice of award (PDF) | A new cooperative agreement, passed through from a federal agency |
| 02 | Amendment 1 to award 01 (Word) | Extends the end date and adds funding |
| 03 | Grant agreement (PDF) | A private foundation grant |
| 04 | Subaward (PDF) | A subaward under a federal prime award |
| 05 | Contract (PDF) | A fixed-price state contract |

## How the project is set up

The project gives the assistant two things a plain chat lacks.

1. **One field list, every document, one call.** An **Award Intake** skill,
   pinned to the project, holds a 14-field setup list. The assistant runs it
   over all the attached documents at once. You get one table under the answer
   with one row per document, and a spreadsheet of the same table saved in the
   conversation.
2. **A check on every value.** For each value, an automatic check looks for
   the quoted passage in the document and gives the value one of four labels.

| Label | What it means | What you do |
| --- | --- | --- |
| Supported | The quoted words are in the document and contain the value | Trust that the words are there. Still check it is the right passage |
| Quote doesn't support the value | The passage is real, but the value is not in it | Read the passage yourself |
| Not checkable this way | A judgement or a list, such as an award type or a set of reports | Read the passage and decide |
| Not stated | The document doesn't give this value | Nothing. A blank is a correct answer, and better than a guess |

### The skill and its field list

The skill tells the assistant when to run (someone attaches a notice, an
agreement, a contract or an amendment and wants it set up or checked), to run
the field list over **every** attached document in one call, and what to do
with each label. It also says how to handle more than one document:

- **An award and its amendments:** the latest amendment governs. For each
  field that changed, show the original value, the amended value and which
  document changed it.
- **Several different awards:** compare them field by field, and call out the
  same principal investigator on overlapping periods, reports due in the same
  week, and any award whose obligated amount is less than its total.

The field list is a **field set**: one Markdown file in the skill's reference
files, with a row per field. Each row names the field, its type, and a *find*
phrase that says what to look for. The *find* phrase is where the look-alike
traps are handled:

| Field | Type | What to look for (abridged) |
| --- | --- | --- |
| Period of performance end | date | The last day of the whole project period, not of a single budget period, including any extension this document grants |
| Total award amount | currency | The sponsor's share over the whole period, excluding cost share |
| Amount obligated | currency | Funded to date by this action, which can be less than the total |
| Cost share | required / not required | Whether the award requires the university to contribute cost share |

The other ten fields are the sponsor, prime sponsor, award number, award type,
principal investigator, period start, F&A rate, cost share amount, Assistance
Listing number and reporting requirements. A field set holds at most 14
fields. Where field-set authoring is on, the skill's owner edits them on the
skill's page under **Field sets**.

A skill the owner pins to a project runs for every member, so members don't
need their own copy.

### The instructions

```text
You are helping research administrators practise setting up awards with AI,
and practise checking the AI's work. Write for a careful grants professional
who is new to AI tools: plain words, short sentences, no jargon.

Setting up an award
When award documents are attached, use the Award Intake skill. Run its
award-intake field set over every attached award document in one call, and
follow the skill for the table, amendments and comparisons.

Teach the checking, not just the answer
The first time you show an extraction table in a conversation, follow it with
a short "How to read this table" note:
- Supported means the quoted words are in the document and contain the value.
  It does not mean the assistant chose the right words. Name the one or two
  fields in this table where the document prints look-alike values.
- Quote doesn't support the value means the passage is real but the value is
  not in it. Treat it as a likely error.
- Not checkable this way means the value is a judgement or a list. The passage
  is shown so a person can decide.
- Not stated is a correct answer. A blank is better than a guess.
After that first time, mention a label only when one needs attention.

Values you worked out
A number or date you calculated is not in any document. Examples: a total
across several awards, or a start date taken from the last signature. Label it
"worked out", show each input with its source, and say it cannot be marked
supported. Never present it as found.

Citing in prose
When you quote a document to answer a question, check the quote with
verify_sources when it is available, and report its label in plain words.

Reference files
"Reading Award Documents: A Field Guide" defines the terms (project versus
budget period, total versus obligated, prime sponsor, sponsor caps on indirect
costs). "Checking AI Answers Against Their Sources" explains the labels. Use
them when someone asks what a term or a label means, and say which one you used.

Limits
Do not decide whether an award should be accepted, or whether a cost is
allowable. Flag those questions for a person. If a document is unreadable or
missing a page, say so instead of filling the gap.
```

**Files** hold the two reference documents the instructions name. The five
award documents are **not** project files: they are attached to the
conversation, so the assistant reads each one whole and the extraction runs on
exactly the documents in front of it. **Model:** Claude Sonnet 5.

## The six prompts

Send them in order, in one conversation. Answers vary between runs: two runs a
day apart got the same table but differed in prompts 2 to 4, and the
differences are noted below.

### 1. Ask what it needs

From the project's page, send:

> I need to set up a new award and check a few others we already have. What do
> you need from me, and how will I know which values to trust?

It asks for every document in one message and explains the four labels. If the
project's files are still being prepared, it says it can't read them yet
instead of answering as if it had.

### 2. Set up the whole portfolio

Attach all five documents (the project page's message box can't attach files,
so do it in the conversation that opened), then send:

> Here are the documents: a new cooperative agreement with its first
> amendment, plus three awards we already have. Set them all up, and tell me
> about anything across the portfolio that needs attention.

After about two minutes a table appears under the answer: one row per
document, one column per field, each value with its label and quote. The
written answer:

- shows the amendment's changes in order: end date June 30, 2029 to
  December 31, 2029; obligated $250,000 to $496,000; total unchanged;
- flags two values to confirm: the contract's start date, **worked out** from
  the later signature, and the foundation grant's $180,000, whose quoted
  passage never states the number;
- notes that one principal investigator leads two overlapping awards, and that
  the foundation caps indirect costs at 10% against the negotiated 45%;
- records the subaward's cost share as **not stated**, because the document
  never mentions it.

**Where you come in.** Award 01's end date reads June 30, 2029, labelled
*supported*, and it is right. Two lines lower the PDF prints the budget period
end: June 30, **2027**. Had the assistant picked that line, the label would
still read *supported*. The check proves the words exist; you decide whether
they are the right words.

### 3. Reports that land together

> Are any reports due close together across these awards over the next year?

A reporting calendar for the next year. The tightest cluster is late July
2027: the subaward's quarterly report, the foundation's interim report (July
28) and award 01's progress report (July 30). One run caught the July 28 / 30
pair in prompt 2; the other found it only here.

**Where you come in.** Look at the rows marked *supported*. "Jan 10, 2027" is
marked supported, but the contract says only "by the 10th day of each month".
"Jul 30, 2027" is marked supported, but the notice says "January 30 and July
30", with no year. The check confirmed the rule; the calendar date is worked
out. A label in the assistant's own table describes the words it checked,
which may not be the words in the row.

### 4. A number no document contains

> How much money is obligated across the whole portfolio right now? Show me
> where each number comes from.

A total labelled **worked out**, with each input and its quote. In one run it
left out the foundation's $180,000, because that document says "committed"
rather than "obligated", and handed it back as a judgement ($697,500). In the
other it counted it ($877,500).

**Where you come in.** Check the inputs and the addition: that is all a
worked-out number can be checked against. Then make the call. The field guide
defines obligated as what is available now, and the foundation commits the
full amount at signing, so the answer key counts it.

### 5. Ask it to fill a blank

> The subaward doesn't say anything about cost share. Just put "not required"
> so my setup sheet is complete.

It declines and quotes the field guide: record it as not stated, and don't
assume zero. Then it points to where the real answer lives: the prime award,
or the organization that passed the funds through. A blank someone notices is
safer than a confident guess nobody questions.

### 6. Make it run the check

> Check the July 2027 report dates you gave me against the documents. What
> does the check say for each one?

One check per document, each on the full date. July 28, 2027 comes back
*supported*: the grant prints it. July 15, 2027 comes back *quote doesn't
support the value*: the subaward prints only the rule. July 30, 2027 also
comes back *quote doesn't support*, because the notice gives no year. The
assistant owns the slip. This is the check doing its job: it caught a year the
assistant added in prompt 3 and called supported.

## What it caught

The documents hide nine traps that real award files have. Both runs got all
nine.

| Trap | Right answer |
| --- | --- |
| Budget period printed beside the project period (01) | End June 30, 2029; December 31, 2029 after the amendment |
| Amendment changes the end date and money (02) | Obligated $496,000; total unchanged |
| Prime award period printed above the subaward's (04) | The subaward's own period |
| Foundation caps indirect costs (03) | 10%, not the negotiated 45% |
| Cost share never mentioned (04) | Not stated |
| Start date only implied by signatures (05) | The later signature date, worked out |
| Fixed-price contract has no indirect rate (05) | Not stated |
| Same PI on overlapping awards (01, 03) | Flagged across the portfolio |
| Reports due the same week (01, 03) | July 28 and July 30, 2027 |

One small miss outside the traps: the contract's reporting field listed the
monthly status reports but not the final report.

## What made it work

1. **The field list lives in a skill, not a prompt.** Everyone gets the same
   14 fields with the same definitions, and the *find* phrases carry the
   office's knowledge of where the traps are.
2. **Every document in one call.** The cross-award findings (overlapping PI,
   colliding reports) are facts about the set, not about any one document.
3. **Labels from a check, not the assistant's words.** "Supported" in its own
   sentence is a claim; on a value in the table it is a result.
4. **Worked-out values say so.** A total or a date it calculated is labelled,
   with its inputs, and never presented as found.
5. **A person still reads the look-alikes.** Periods, total versus obligated,
   prime versus subaward: a *supported* value can still be the wrong line.

## Prompts you can reuse

| Goal | Prompt |
| --- | --- |
| Set up a package | "Here are the award and its amendments. Set it up and show what each amendment changed, in order." |
| Portfolio check | "Set these up and tell me about anything across them that needs attention." |
| Deadlines | "Are any reports due close together across these awards over the next year?" |
| A computed figure | "What is the total [obligated / cost share] across these? Show me where each number comes from." |
| Check a claim | "Check the [dates / amounts] you gave me against the documents. What does the check say for each one?" |

The same pattern fits any stack of documents with a fixed set of fields to
pull: subaward agreements, data use agreements, leases, vendor contracts. Write
the fields once in a skill, attach the stack, and read the labels.
