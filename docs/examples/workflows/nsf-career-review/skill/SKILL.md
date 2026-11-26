---
name: career-proposal-review
description: "NSF CAREER proposal review: four reviewer Agents, a 114-criterion compliance audit and a panel summary, built into a marked-up PDF of the proposal."
---

# CAREER proposal review

This skill carries the **CAREER proposal review** workflow (`references/workflow.career-proposal-review.md`): four reviewer Agents and a 114-criterion compliance audit (15 field sets) read one NSF CAREER proposal draft, a summary step merges them, and the result comes back here to be built into a marked-up PDF.

## Building the marked-up review

Use this when the workflow's results arrive (the panel's notes from `review_summary` and the audit's verdicts) and you are asked to build the review.

1. **Write `review.json`** with one `run_code` call (code you write, `json.dump`), in this shape:

```json
{
  "recommendation": "Competitive: one sentence why",
  "fix_first": ["most serious change", "..."],
  "notes": [
    {"n": 1, "section": "Project Summary", "quote": "the exact quoted phrase",
     "note": "what to change", "reviewers": "Generalist, Emotional Outsider"}
  ],
  "audit_fails": [
    {"section": "Formatting", "criterion": "Project Description within 15 pages",
     "page": 18, "note": "what to do"}
  ],
  "audit_counts": [
    {"section": "Eligibility", "pass": 8, "fail": 0, "cant_tell": 0, "not_applicable": 0}
  ]
}
```

   - `notes`: every numbered inline note from the panel summary, its quote copied exactly as the summary wrote it.
   - `audit_fails`: every audit criterion whose verdict is `fail`, with `page` from the audit's **Where** column (`p. 18` is `18`). Add a `quote` only if the panel quoted the passage exactly; never write one yourself.
   - `audit_counts`: one row per audit section, counted from the audit's verdicts (all 15 sections).

2. **Run the script**: `run_code` with this skill's name, `path: "scripts/build_review.py"`, `files: ["<the proposal PDF's file name>"]`, and `argv: ["inputs/<the PDF's file name>", "review.json", "outputs/<name without .pdf>_reviewed.pdf"]`. Never copy the script into `code`.

   The script finds each quote in the PDF, highlights it, adds a comment annotation in the margin, and puts a **Panel review** page first with the recommendation, the fixes, every note with its page, and the audit counts. It prints where each note went.

3. **Reply briefly**: the recommendation in one line, the Fix first list, a table of each note and the page it landed on (or "not found"), and the audit failures. The PDF carries the detail.

If a note is "not found", say so; don't re-run with a made-up quote.
