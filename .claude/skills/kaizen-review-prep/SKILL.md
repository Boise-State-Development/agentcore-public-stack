---
name: kaizen-review-prep
description: Friday late-morning synthesis. Runs ~2 hours after `kaizen-research` the same morning. Consumes this week's research doc, open items in `docs/kaizen/review-queue.md`, last weekend's POC findings (from comments on the previous week's research PR), and recent merges/reverts/CI signal — produces a ranked, decision-oriented agenda. Every item has a Ship / Decline / Defer recommendation. Opens a PR into `develop`. Triggers: "kaizen review prep", "weekly review prep", "friday review", "rank kaizen ideas".
---

# Kaizen Review Prep

Friday late morning, after `kaizen-research` ran earlier the same morning. This skill consolidates this week's research + open queue items + last weekend's POC findings (lifted from PR comments on the previous week's research PR) + recent repo state into a ranked decision agenda. Phil reviews Friday morning, marks ✅/❌/⏸ on each item, ships 1–3 the following week, and POCs the next batch over the weekend.

## Philosophy

- **Review is a decision forum, not a status update.** Everything that lands in the output should be either: (a) actionable this week, (b) explicitly deferred with a reason and revisit date, or (c) declined. Nothing is "noted." Noted-and-forgotten is how systems accumulate friction.
- **Subtraction first.** Every proposal ranks against "do nothing" and "retire something instead." If a proposal adds anything, it must explain what existing thing it either replaces or simplifies.
- **Dual lens — impact + capability-unlock.** Rank proposals through *two* lenses, not one: (a) **impact on existing code** (does this change, simplify, or obsolete something we already have?) and (b) **capability unlock** (what *new* product capability or UX enhancement does this enable that we couldn't easily build before?). Subtraction-first applies to lens (a). But proposals that genuinely unlock new product surface — code-interpreter sandboxes, persistent agent state, multi-agent UI attribution, new SSE event types that enable inline UI, etc. — must be evaluated on their strategic merit, *not* auto-deferred because they don't intersect existing code. A proposal with no `Subtracts` value but a substantive `Unlocks` value can rank above a low-impact dep-bump. Don't penalize net-new capability for not being a cleanup.
- **Multiple cycles.** Kaizen is small changes, weekly, compounding. If this week's review touches 3 things, next week's will touch 3 different things. Phil doesn't need a grand plan — he needs a reliable weekly cadence.
- **One-week feedback lag is intentional.** Phil reviews Friday → POCs over the weekend → those POC findings surface in the *next* Friday's review-prep as Carried Over items. Don't try to fold same-day POC findings in — they don't exist yet.
- **No edits outside `docs/kaizen/`.** This skill writes one Markdown file under `docs/kaizen/reviews/` and updates `docs/kaizen/review-queue.md` (moves Open → Resolved post-review). It never touches source code, `CLAUDE.md`, or skill files. Those changes happen in separate PRs after the review.

## When to run

Friday late morning (~8am MT), ~2 hours after `kaizen-research` runs. Phil reviews both docs Friday morning, picks 1–3 to ship over the coming week, and POCs additional items over the weekend. POC findings from last weekend's POC session surface here as Carried Over items (lifted from PR comments on the *previous* week's research PR — not this week's, which Phil hasn't seen yet).

## Inputs

1. **Most recent `docs/kaizen/research/YYYY-MM-DD.md`** — Friday's scan. Its Top 5 ideas are the primary candidate list.
2. **`docs/kaizen/review-queue.md`** — `## Open` entries. Includes both this week's ideas (just appended by `kaizen-research`) and any prior-week items that weren't resolved.
3. **Last 1–2 `docs/kaizen/reviews/*.md`** — what was proposed before, what was decided, anything deferred to "revisit by [date]".
4. **PR comments on the *previous* week's kaizen-research PR.** `gh pr view <n> --comments` — Phil's reactions and weekend POC findings are first-class signal. The PR opened *this* morning by `kaizen-research` is too fresh; comments accumulate over the week as Phil POCs ideas. Pick the research PR from one week ago (or the most recent merged/closed kaizen-research PR), not today's.
5. **`docs/kaizen/decisions.md`** (if it exists) — declined items with reasons. Don't re-propose without materially new context.
6. **Recent activity since last review:**
   - `git log develop --since="<last review date>" --oneline --no-merges` — what shipped.
   - `gh pr list --base develop --state merged --search "merged:>$(date -v-7d +%Y-%m-%d)"` — what landed.
   - `gh run list --status=failure --limit 30` — fresh CI failures.
7. **`CLAUDE.md` + skill inventory** — surface concerns only; never propose unilateral edits to these.
8. **`CHANGELOG.md` / `RELEASE_NOTES.md`** — most recent ~14 days, for the "what shipped this week" celebration block + the don't-re-propose filter.

## Output

### 1. Review doc — `docs/kaizen/reviews/YYYY-MM-DD.md`

```markdown
# Kaizen Review — [Day, Month D, YYYY]
> Prepared HH:MMam MT. Review window: [Month D – D] (7 days).
> Source: research/YYYY-MM-DD.md + review-queue.md (N open items).

## Week in Review

[2-4 sentences. What did the week reveal about the system? Use concrete language —
"The aws-samples reference repo introduced a new agent-loop pattern and we're 2
Strands releases behind" beats "some external changes". This is Phil's pulse
check before decisions.]

## Friction — the week's signal

### Repeated patterns (≥2 occurrences)
- **[Pattern]** (N times) — [concrete description; quote PR review comments or commit messages where helpful]
  - *Hypothesis*: [root cause]
  - *Candidate fix*: [specific change — file + behavior]

### One-offs worth watching
- **[Pattern]** (1 occurrence) — [context]

### Silence that matters
<!-- What WASN'T used: skills not referenced this week, features not invoked,
     CI workflows that haven't run, etc. -->
- **[Silence]** — [what wasn't used + what that might mean]

## Proposals — ranked

<!-- 5–10 items. Each is a DECISION for Phil, not a status. Every item has
     a specific Ship option, a specific Decline option, and a recommendation. -->

### 1. [Proposal title]
- **Source**: research/YYYY-MM-DD.md ▸ Top 5 #N | review-queue.md (open since YYYY-MM-DD) | PR comment | direct observation
- **Surface area**: backend / frontend / infrastructure / cross-cutting / docs / skills
- **Change**: [concrete description — what files change, what the new behavior is]
- **Subtracts**: [required field — what this retires, simplifies, or replaces. Or explicitly "addition only — justified because…"]
- **Unlocks** (if applicable): [net-new product capability, UX pattern, or enhancement this enables — bulleted if multiple. Required for proposals where `Subtracts: no — addition only`; the unlock is the justification. Omit when purely a cleanup/dep-bump and not applicable.]
- **Effort**: Low / Med / High
- **Impact**: Low / Med / High
- **POC findings (if Phil tried it)**: [summary or "not POCed"]
- **Ship means**: [specific action — "open PR updating X to do Y" or "retire skill Z"]
- **Decline means**: [what happens instead — usually "keep current behavior, revisit in N weeks"]
- **Recommendation**: Ship / Decline / Defer N weeks — [one-sentence why]

### 2. [Next proposal]
…

## Carried Over From Prior Reviews
<!-- Items deferred in earlier review docs that have hit their revisit date.
     Surfaced here for re-decision so nothing rots silently in "deferred" status. -->

- **[Deferred item]** (deferred YYYY-MM-DD until YYYY-MM-DD) — [original context]. Now due.

## Retirement Candidates

<!-- Things currently in the scaffold that aren't earning their place.
     Bias strongly toward subtraction. If you can't find anything, that's a
     finding — flag it in the Take. -->

- **[Candidate]** — [evidence: not modified in N days, not referenced, replaced by X]

## Risks Acknowledged But Not Acted On
<!-- From research's "Risks introduced this week" section. Surface so Phil
     can decide: address now, defer with a watch date, or accept. -->

- **[Risk]** — [source URL] — *what breaks if ignored* — recommendation: [Address now / Watch until [date] / Accept]

## What Shipped This Week

<!-- From CHANGELOG.md / merged PRs. Short list, one line each. Context for
     "the system absorbed this much change recently — propose less if a lot." -->

- [shipped item] — *why it mattered*

## Take

[2-4 sentences. Is the system trending toward trust or toward friction? Is the kaizen
loop catching real signal or generating noise? What's the one change that would
matter most this week if shipped? Don't sugarcoat — if a skill or pattern isn't
pulling its weight, say so.]

---

## Review Protocol (for Phil)

1. Read Friction (2 min).
2. Scan Proposals — mark ✅ Ship / ❌ Decline / ⏸ Defer on each (3-5 min).
3. Scan Retirement Candidates — same marks (1-2 min).
4. Resolve Carried Over items (1-2 min).
5. Resolve Risks block.
6. Pick 1-3 to ship this week. Decline or defer the rest with a reason.

Target: 10-15 minutes.

## Post-review (for Phil — separate PRs)

- ✅ Ship items → individual feature PRs over the week. The decision is logged in this doc; the implementation lives elsewhere.
- ❌ Decline items → appended to `docs/kaizen/decisions.md` with Phil's reason so future research doesn't re-propose.
- ⏸ Defer items → kept open in `review-queue.md` with a "revisit by [date]"; surface again in the next review when due.

This skill produces the agenda. Implementation never happens here.
```

### 2. Queue update — `docs/kaizen/review-queue.md`

After Phil reviews and the decisions are logged in the review doc, this skill (or Phil himself, manually) **moves resolved items** from `## Open` to `## Resolved` with a Decision and Reasoning. On a fresh run before Phil has reviewed, the skill leaves Open as-is — only the *prior* review's outcomes get processed for queue movement.

## How to run

1. **Bootstrap.** Confirm `docs/kaizen/reviews/` exists; create it if not.

2. **Read inputs** (sequential — small reads):
   - Latest file in `docs/kaizen/research/`
   - `docs/kaizen/review-queue.md` (full)
   - Last 1–2 files in `docs/kaizen/reviews/`
   - `docs/kaizen/decisions.md` if present
   - Last ~14 days of `CHANGELOG.md` and `RELEASE_NOTES.md`
   - `CLAUDE.md` (read-only — for context, not edits)

3. **Pull PR comments on the latest research PR** (parallel with step 4):
   ```
   gh pr list --base develop --state all --search "kaizen/research" --limit 1 --json number,url
   gh pr view <number> --comments
   ```
   Capture Phil's reactions. POC findings he mentions get folded into proposal entries.

4. **Pull recent activity** (parallel Bash):
   - `git log develop --since="<last review date>" --oneline --no-merges`
   - `gh pr list --base develop --state merged --search "merged:>$(date -v-7d +%Y-%m-%d)" --limit 30`
   - `gh run list --status=failure --limit 30`
   - `gh issue list --state open --search "created:>$(date -v-7d +%Y-%m-%d)"`

5. **Process prior-review queue movement.** For each entry in `## Open` that was resolved in the most recent review doc, move it to `## Resolved` with the Decision + Reasoning + Reviewed-in fields. Items with no decision in the prior review stay open.

6. **Identify Carried Over items.** Scan prior review docs for `Defer N weeks` recommendations whose revisit date has hit. Add those to the new review's Carried Over section.

7. **Synthesize the review doc** per the shape above. The Proposals list is built from:
   - All `## Open` entries in `review-queue.md` (the primary source)
   - Any new friction patterns surfaced from PR comments / merged PRs / CI that weren't already in the queue
   - Carried Over items
   Rank:
   - Low-effort × High-impact first.
   - **Retirement candidates** get a +1 boost (subtraction bias).
   - **Capability-unlock items** (proposals with a substantive `Unlocks` field — new product capability, UX surface, or platform primitive adoption) rank on their strategic merit. Do not auto-defer just because `Subtracts: no`. A High-impact unlock can rank above a Low-impact subtraction.
   - Items with **POC findings** rank above untested items at the same effort/impact.

8. **Cap the proposal count at 10.** If more than 10 candidates, defer the lowest-ranked to next week with a note. The review is supposed to take 10-15 minutes, not be exhaustive.

9. **Open a PR** — see "PR creation".

## PR creation

```bash
DATE=$(TZ=America/Denver date +'%Y-%m-%d')
BRANCH="kaizen/review-${DATE}"

git checkout -b "$BRANCH" develop
git add docs/kaizen/
git commit -m "$(cat <<EOF
chore(kaizen): weekly review prep ${DATE}

Generated by kaizen-review-prep. Ranked agenda for the 10-15 min decision pass;
queue updated with prior-review outcomes.
EOF
)"
git push -u origin "$BRANCH"

gh pr create --base develop --head "$BRANCH" \
  --title "chore(kaizen): weekly review prep ${DATE}" \
  --body "$(cat <<'EOF'
## Summary
- N proposals ranked Effort × Impact (retirement candidates boosted).
- Friction patterns from the week's commits, PRs, and CI surfaced.
- Carried-over deferred items now due for re-decision.
- POC findings (from kaizen-research PR comments) folded into proposals where Phil tried something.

## Review
1. Read Friction (2 min).
2. Mark each Proposal: ✅ Ship / ❌ Decline / ⏸ Defer.
3. Same for Retirement Candidates and Risks.
4. Pick 1-3 to ship this week.

Target: 10-15 minutes.

## Decision
Ship the doc to `develop`. Action on individual items happens in separate PRs over the week.
Declined items go to `docs/kaizen/decisions.md`; deferred items stay open in `review-queue.md` with a revisit date.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Rules

- **Every proposal is a decision.** No "consider X" or "we might want to." Each has Ship means / Decline means / Recommendation.
- **Every proposal has a Subtracts field.** Required. If empty, ask: does it really need to be its own thing? Could an existing skill / construct be updated instead? If still pure addition, justify it explicitly.
- **Retirement candidates are required.** If the section is empty and the system has been running >2 weeks, *that's* the finding — flag it in the Take.
- **Don't re-propose declined items** without materially new context. Cross-check `docs/kaizen/decisions.md` and the last 1–2 reviews.
- **Carried Over is not a graveyard.** Deferred items resurface on their revisit date. No silent deferrals.
- **No fabrication.** If a week was quiet, the review is short. Length tracks signal, not target word count.
- **Never edit `CLAUDE.md` or skill files unilaterally.** A proposal can recommend a change to them, but the change itself is always Phil-approved in review and shipped in a separate PR.
- **Cap at 10 proposals.** A 15-item list defeats the 10-15 min target.

## Confirmation

After the PR is opened, tell Phil:
1. PR URL.
2. Top 1–2 proposals (title, Effort×Impact, recommendation).
3. Top 1 retirement candidate if any.
4. One-sentence Take.
5. Estimated review time.

Brief. Phil reads the full doc on the PR and marks decisions there or in a follow-up commit.
