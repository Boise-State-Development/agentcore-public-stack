# CAREER proposal review: a saved-workflow example

One saved workflow that reviews a draft NSF CAREER proposal and returns the proposal as a marked-up PDF. Four
reviewer Agents read it side by side. A 114-criterion compliance audit runs beside them, and a summary step
merges everything into notes the PI can act on. The conversation that started the run then builds the PDF: a
Panel review page first, then the proposal with each note highlighted and each failed audit criterion flagged
on its page.

It reproduces a multi-agent proposal reviewer a graduate student built in LangGraph: seven kinds of node,
including four personas, an audit fanned out over 15 sections, a summary and a PDF build. Here that graph is
one workflow file in a skill, started from the composer, with no orchestration code. It is a worked example
for `docs/specs/workflow-designer.md`, and it exercises all three step kinds: Agent, checklist and prompt.

Everything in the proposal is fictional: the PI, the institution, the forest and the letter writers.

## What's here

```
nsf-career-review/
  README.md
  agents/                        the five Agents to create, with their instructions
    generalist.md  colleague.md  emotional-outsider.md  fiscal-outsider.md
    review-builder.md            the Agent you start the run from
  skill/                         the career-proposal-review skill, as an importable bundle
    SKILL.md                     how the Builder turns the results into the PDF
    references/
      workflow.career-proposal-review.md   the workflow (6 steps, result: merge)
      field-set.<section>.md               the audit: 15 field sets, 114 criteria
    scripts/build_review.py      places the notes on the PDF (pdfplumber, pypdf, reportlab)
  proposal/
    marsh_career_draft3.pdf      the 32-page test proposal, with seven planted problems
    marsh_career_draft3.html     its source
    answer-key.md                the seven plants, the page map and all 114 expected verdicts
    audit-answer-key.md          the 114 expected verdicts as one table (generated)
  tools/
    criteria.py                  generates the field sets and audit-answer-key.md
    score_audit.py               scores a run's audit against the key
```

Keep the PDF as committed. The page numbers in both answer keys and in plant A (a 16-page Project Description)
are this render's, and regenerating it from the HTML can move them.

## How the student's graph maps onto it

| Their node | Here | Step kind |
|---|---|---|
| `ingest` | the attached PDF; every step reads it natively, pages kept | no step |
| Generalist, Colleague, Emotional Outsider, Fiscal Outsider | four Agent steps, side by side | Agent (a conversation each) |
| `pull_criteria` | the 15 field sets in the workflow's own skill | data, versioned with the skill |
| `audit_section` × 15 | one checklist step naming 15 field sets: 15 calls over one cached copy of the PDF | checklist |
| `aggregate_audits` | the checklist's per-set results, counted by the summary and the build | no step |
| `review_summary` | a prompt step reading the four reviews **and the audit** | prompt |
| `build_review` | the merge turn: the Builder runs `scripts/build_review.py` | merge |

One deliberate change: in the student's graph the audit's result is never used. Here the summary reads it, so
the three problems only an audit can catch (A, B and G) reach the PI and the PDF.

## What a deployment needs

- **Flags (backend and SPA):**
  - `HANDOFFS_ENABLED`, `WORKFLOW_RUNS_ENABLED` and `WORKFLOWS_ENABLED`
  - `PROMPT_STEPS_ENABLED` and `CHECKLIST_STEPS_ENABLED`
  - `CODE_SANDBOX_ENABLED`, with the `run_code` tool granted to your role
  - `SKILL_BUNDLE_IMPORT_ENABLED` is optional; it lets you import `skill/` in one step.
- **Models:** catalog rows for Claude Sonnet 5.5 and Claude Haiku 5.5 (the `us.*` ids), both granted to your
  role.
- **Size:** the proposal is about 80K input tokens as a native PDF (1,565 a page plus its text). That runs on
  Sonnet 5.5 but not on Haiku 5.5, whose single-call budget stops at its 100K long-context line. That is why
  the audit and the summary run on Sonnet.

## Setting it up

1. **Create the four reviewer Agents** from `agents/generalist.md`, `colleague.md`, `emotional-outsider.md`
   and `fiscal-outsider.md`: name, model and the instructions block as written. They need no tools or files.
2. **Create the skill** from `skill/`:
   - With bundle import on, import the folder: a zip of `skill/`, or this folder's public GitHub path.
   - Otherwise, create a skill named `career-proposal-review` with SKILL.md's body as its instructions. Upload
     the 16 `references/` files as references and `build_review.py` as a script.
3. **Point the workflow at your Agents.** The workflow file names its four Agent steps with placeholders
   (`your-generalist-agent-id` and so on). Open **CAREER proposal review** on the Workflows page and choose
   your Agent for each reviewer step, or edit the file's table. The audit and summary steps need no change.
4. **Create the Builder** from `agents/review-builder.md`: Sonnet 5.5, the skill bound, and `run_code`.
   Binding the skill pins its current version. After any later change to the skill, update the Builder to
   the new version from the skill's page.

## Running it

1. Open a conversation with **NSF Review Builder** and send one message first. The starter "What happens when I
   start the review?" works. Starting the workflow from a brand-new Agent page opens it in a separate
   conversation, without the Agent or the file.
2. Attach `proposal/marsh_career_draft3.pdf`.
3. Composer **+** → Workflows → **CAREER proposal review**. In a narrow window the submenu can open
   off-screen; widen it first.
4. On the review sheet, **tick the PDF**: the sheet lists it unticked. The estimate updates (about $2 to start)
   and Start becomes available. The sheet shows 6 steps, the audit's 15 calls, and "The merge starts by itself…".
5. Leave the composer empty. The merge starts by itself only while this conversation is open, idle and has
   nothing typed.
6. Watch the run row: the four reviewers and the audit side by side, then Review summary, then the merge.
   The Builder replies with the recommendation, the Fix first list, where each note landed, and the audit
   failures. `marsh_career_draft3_reviewed.pdf` arrives as a download card.

## Scoring a run

The seven plants (`proposal/answer-key.md`):

| Plant | Problem | Caught by |
|---|---|---|
| A | Project Description is 16 pages against a 15-page limit | the audit |
| B | Senior salary is 3.0 months a year, unjustified | the audit (and usually the Fiscal Outsider) |
| C | The Project Summary opens with methods; the stakes arrive last | reviewers |
| D | A key term is used four times before it is defined | reviewers |
| E | 18 wells and no effect size or power analysis | reviewers |
| F | Drone LiDAR is bought in Year 1 but first flown in Year 2 | reviewers |
| G | The Data Management Plan names no repository | the audit |

To score the audit, save the run's JSON while signed in (`GET /api/sessions/{sessionId}/workflow-runs/{runId}`)
and run:

```bash
python3 -I tools/score_audit.py run.json
```

It prints right, wrong and missing counts against the 114 expected verdicts, each audit plant's verdict, the
provenance states, and every miss with its quote. The key accepts two answers where a draft supports either.
For example, program income is `pass` or `not_applicable`, because the proposal says it expects none.

To change the audit, edit `tools/criteria.py` and run `python3 -I tools/criteria.py`. It rewrites the field
sets in `skill/references/` and `proposal/audit-answer-key.md`; then upload the changed sets to the skill.

## Results on our dev deployment (2026-11-26)

| | First run | Last run |
|---|---|---|
| Time, start to PDF | ~4 min | ~4 min |
| Cost | $0.70 (no audit) | $1.37: audit $0.54, reviewers $0.54, summary and merge $0.29 |
| Audit | stopped `too_large` | 114/114; A, B and G `fail` on p. 18, 24 and 30 |
| Items placed on the PDF | 15 of 15 notes | 21 of 21: 18 notes by exact quote, 3 audit failures on their pages |
| Plants in the notes | E | A, B, E, F and G; C and D partly |

The audit's 15 calls took 36 s. The first wrote the 80.7K-token proposal to the prompt cache and the other 14
read it from there.

Building this example turned up two gaps in workflow steps, both now fixed:

- **A real-size PDF couldn't be read by a prompt or checklist step.** Their document budget was half the *chat*
  compaction ceiling (25K on Haiku 5.5, 50K on Sonnet 5.5), so every realistic proposal stopped `too_large`.
  It is now 0.75 × the model's input window or its long-context line, whichever is lower ([PR 2462](https://github.com/Boise-State-Development/agentcore-public-stack/pull/2462)).
- **Later steps couldn't see where a checklist value was found.** A checklist step's answer carried values and
  states only, so the merge knew a criterion failed but not where, and placed none of the three failures on the
  right page. Each row now carries its page (`| fail | unassessed | p. 18 |`) ([PR 2465](https://github.com/Boise-State-Development/agentcore-public-stack/pull/2465)).

Two prompt changes also mattered. In the first run the summary's 15-note cap, ranked by severity, dropped the
Emotional Outsider's points; the summary now gives each reviewer's two most serious points a note of their own.
A prompt step's stored answer is capped at about 8,000 characters, so the summary is told to stay under 1,000
words, or its last section is cut off.

C and D are still only partly caught. The panel asks for the hook and stakeholders up front, and for the zone
to be defined consistently, but nobody says the summary opens with methods or that the term is used before it
is defined. That gap is in the reviewers' lenses, not the workflow.

## Talking points

- The student's graph needed code for every node and edge. Here it is one workflow file in a skill. It is
  versioned and shared like the skill, and started from the composer.
- The reviewers are ordinary Agents, each with its own conversation the PI can open and keep talking to.
- The 15 audit sections are 15 field sets, so one checklist step runs them in 15 calls over one cached copy of
  the PDF. Every verdict carries a quote, a page and a provenance state.
- The summary reads the audit, so compliance failures reach the PI next to the reviewers' points.
- The PDF is built by a skill script in the sandbox, not by the model retyping code. Notes are yellow
  highlights with margin comments, and audit failures are red comments on the page the audit cited.
