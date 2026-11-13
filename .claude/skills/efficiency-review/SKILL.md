---
name: efficiency-review
description: >-
  Weekly cross-pillar review of a deployment: cost, speed and answer quality
  joined on the turn, so an efficiency is a gain on one pillar that costs the
  others nothing. Scans the per-call cost rows and thumb rows (read-only,
  content-free), runs scripts/efficiency_review.py, compares the last 7 days
  with the 7 before and with the previous run, and ends in a short ranked list
  of candidates with what each does to all three pillars. Use when the user
  says "efficiency review", "weekly efficiency", "find efficiencies", "where
  are we wasting time or money", "cost, speed and quality this week", or wants
  to repeat the cross-pillar analysis.
---

# Efficiency review

Three pillars, one unit. Cost, speed and quality are each measured somewhere
already; this skill joins them on the **turn** and slices by model, release,
model calls per turn, tool, surface and documents. A win on one pillar that
costs another is a trade, not an efficiency, so every candidate is reported
against all three.

- `scripts/efficiency_review.py`: builds every table and the mechanical leads. Stdlib only, run with `python3 -I`.
- `docs/efficiency-reviews/<end date>.json` and `.md`: one pair per run. The newest `.json` is the next run's `--previous`.

It sits beside two other readers and does not replace them:

| Question | Reader |
|---|---|
| Did this release regress? | `/release-readout` (release ledger, candidate vs baseline) |
| What kind of usage carries the spend? | `/usage-patterns-dashboard` (patterns × cost mechanism) |
| Where can we gain on one pillar without paying on another? | this skill |

## 0. Settle the inputs

| Input | Default | Notes |
|---|---|---|
| AWS profile and table prefix | ask | The deployment's read-only profile (memory may name it); `<prefix>-sessions-metadata` |
| Window | 28 days ending today (UTC) | The headline compares the last 7 days with the 7 before; the tables use all 28 so the floors clear |

Work in a fresh scratchpad directory: `RAW=<scratch>/eff/raw`, `OUT=<scratch>/eff/out`.
Raw scans stay there. Never copy them, or any id from them, into the repo.

## 1. Pull the data (read-only)

Run each command **on one line**. A prod read-only guard may refuse multi-line
commands and any scripting runtime on a line that names the prod profile. The
projections are the privacy boundary: never add `title`, `displayText`,
`citations`, `fileName` or any message field.

```bash
aws dynamodb scan --profile <PROFILE> --table-name <PREFIX>-sessions-metadata --filter-expression "begins_with(SK, :c) AND #t >= :d" --projection-expression "sessionId,userId,#t,messageId,#c,tokenUsage,modelInfo.modelId,latency,cacheStatus,cacheGapSeconds,wastedUsd,toolCalls,hasDocuments,turnDurationMs,compactionEvents,turnAgentId,projectId,agentSwitched,modelSwitched,rewound,outcome,emptyAnswer,appVersion,callKind,usageEstimated" --expression-attribute-names '{"#t":"timestamp","#c":"cost"}' --expression-attribute-values '{":c":{"S":"C#"},":d":{"S":"<START>"}}' --output json > $RAW/cost_rows.json
```

Thumbs, once for `FEEDBACK#down` and once for `FEEDBACK#up` (the reason is a fixed code, never text):

```bash
aws dynamodb query --profile <PROFILE> --table-name <PREFIX>-sessions-metadata --index-name UserTimestampIndex --key-condition-expression "GSI1PK = :p AND GSI1SK >= :d" --projection-expression "sessionId,messageId,#v,reason,retryMessageId,updatedAt" --expression-attribute-names '{"#v":"value"}' --expression-attribute-values '{":p":{"S":"FEEDBACK#down"},":d":{"S":"<START>"}}' --output json > $RAW/feedback_down.json
```

The cost-row scan grows with traffic (about 250 MB for 80K calls); run it in
the background with a 10-minute timeout. If `/usage-patterns-dashboard` ran
today, its scan lacks `latency`, `rewound` and `projectId`; scan again.

## 2. Build

```bash
python3 -I .claude/skills/efficiency-review/scripts/efficiency_review.py --cost-rows $RAW/cost_rows.json --feedback $RAW/feedback_down.json $RAW/feedback_up.json --window-start <START> --window-end <END> --previous docs/efficiency-reviews/<previous end>.json --out $OUT
```

Omit `--previous` on a first run. It prints `review.md`; `summary.json` holds
the same numbers. Neither carries a session or user id.

## 3. Read it, in this order

1. **Coverage.** Each line is the share of turns that carry a signal. A pillar
   whose coverage is low cannot rank anything: say so instead of ranking on it.
   A `—` is under the floor of 20 and is no evidence, never zero.
2. **Week over week.** A move of 10% or more is a lead, not a finding. Check
   the model mix and the turn count before believing it.
3. **The joins.** These are where an efficiency shows:
   - *By model calls per turn* and *by turn length*: where time and cost concentrate together.
   - *Tool turns, with and without an error*: the gap is an upper bound. Long loops have more chances to fail, so never claim the whole difference.
   - *By model*: cost per turn beside turn time and completion. A cheaper, faster model is an efficiency only where quality holds, which needs the eval harness, not this table.
   - *Tools*: a tool's turn cost is the cost of turns that used it, not the cost it caused.
4. **Cache.** `miss_ttl_expired` is human pace and is only reduced by smaller
   contexts (a longer TTL was replayed and loses money; see
   `docs/one-pagers/conversation-usage-patterns.md`). `partial_miss` and
   `miss_avoidable` waste is the unexplained part; rule out `agentSwitched`
   and `modelSwitched` before calling it a defect.

For a release question, also run `/release-readout` on dev and read its
caveats first. For a pattern question, run `/usage-patterns-dashboard`.

## 4. Rank

Write at most six candidates. For each: its 28-day size, and its effect on
cost, speed and quality as *gain*, *neutral*, *trade* or *unknown*. Order them:

1. Pure waste: a gain on one or more pillars, neutral on the rest.
2. Trades that an eval can settle (model routing, result-size defaults, effort).
3. Measurement gaps that block ranking next week.

Before proposing a candidate, check it is not already shipped or parked:
`git log origin/develop`, open PRs, and the parked-levers tables in
`docs/one-pagers/conversation-usage-patterns.md`. Say which candidates are
carried over from the previous run and whether their number moved.

## 5. Record

Copy `$OUT/summary.json` and `$OUT/review.md` to
`docs/efficiency-reviews/<END>.json` and `.md`, and append the ranked list to
the `.md` under `## Candidates`. These files hold aggregates only. Commit them
on a branch from `develop` when the user asks.

## 6. Report

In chat: the coverage caveats in one or two lines, the ranked table, what
moved since the previous run, and the one candidate to act on first.
