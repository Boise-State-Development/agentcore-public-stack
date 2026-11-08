---
name: release-readout
description: >-
  Compare a release (or a dev build) against the one before it from the release
  ledger and say whether it quietly degraded the agent harness: cost per turn,
  cache efficiency, unexplained waste, tool errors and payload, empty answers,
  interruptions, TTFT, eval and smoke results. Runs
  backend/scripts/release_readout.py, reads the caveats-first readout, flags a
  regression only where its n clears the floor, and names where to look next
  (C# rows' cacheStatus and hashes, the session cost anatomy, the per-tool
  payload). Ends in ship, watch or investigate. Content-free. Use when the user
  says "release readout", "/release-readout", "did this release regress", "compare
  1.34.0 with 1.33.0", "verify the release against the ledger", "how did the last
  release do", or when cutting a release (the cutting-a-release skill calls it).
---

# Release readout

The release ledger (`docs/specs/release-ledger.md`) keeps cost and quality per
app version and build. This skill turns its readout into a verdict. **Verify a
release against the ledger, not a smoke run:** a smoke run is a dozen turns on
one build; the ledger is every turn on every build, with the caveats that say
when it cannot tell.

The script computes nothing. Every number and caveat comes from the readout
bundle (`apis/shared/costs/ledger_readout.py` over `ledger_kpis.py`, spec D10);
the script formats it. Your job is the reading, and the traps are in §3.

## 0. Settle the inputs

| Input | Default | Notes |
|---|---|---|
| Environment | dev | Never prod unless the user asks for it **and** `RELEASE_LEDGER_ENABLED` is on there. On dev: profile `dev-ai`, prefix `dev-boisestateai-v2` |
| Candidate | newest version with a full day | `1.34.0`, or one build `1.34.0#<build>` |
| Baseline | the unit before the candidate | Same shape as the candidate (both versions or both builds) |
| Days | 7 | Per window (D5: candidate's first days vs baseline's last days) |

**On dev a version is a release cycle, not a release.** Between releases every
`develop` merge is a new build under the same `VERSION`, so `1.34.0` on dev is
"everything merged since 1.34.0 was cut", and a comparison of one merge against
the merge before it is build against build.

## 1. Run the script

```bash
cd backend
AWS_PROFILE=dev-ai .venv/bin/python scripts/release_readout.py \
    --profile dev-ai --prefix dev-boisestateai-v2 \
    --version 1.34.0 --baseline 1.33.0 --out /tmp/readout-1.34.0
```

It runs the readout route's own handler in process against the deployment's
tables and CloudWatch, so the result is what the Releases tab shows. If the
profile's session has expired, ask the user to run `aws sso login --profile dev-ai`.

Other sources, same output:

- `--export ledger.ndjson` — offline, from `GET /admin/costs/ledger/export`
  (no latency, thumbs, provenance, active users or platform line; the
  markdown says so). Add `--today YYYY-MM-DD` to judge full days as of the
  export's date.
- `--readout-json saved.json` — a readout response saved from the Releases
  tab or fetched in a signed-in browser page (`/api/admin/costs/ledger/readout?...`).
- `--base-url https://<host>/api` with `RELEASE_READOUT_COOKIE` set to a signed-in
  admin's `Cookie` header.

It writes `readout.json` and `readout.md`. Read the markdown first, the JSON
for anything the markdown cuts (all 30 tools, every model's share).

## 2. Read it in this order

1. **Windows.** How many days each side has, how many are full, and whether
   the candidate's window includes today. One partial day is a sample, not a
   release.
2. **Caveats**, every one, before any number. They are generated from a fixed
   set (`CAVEAT_CODES`), so match on the code.
3. **Mix shift.** If `mix_shift` is set, the first explanation of any cost or
   latency delta is the mix, not the code. A model change (Haiku 4.5 → 5.5) or
   a different population of users moves cost per turn on its own; read the
   per-model cost-per-turn table, where like is compared with like.
4. **KPIs and *Moved*.** *Moved* lists KPIs whose relative delta is ≥10% with
   both sides over their floor; the direction word is mechanical. A tiny share
   moving 100% (0.2% → 0.0%) is a different fact from cost per turn moving
   15% on hundreds of turns. Weigh by `n` and absolute size.
5. **Latency, tools, quality**, then agents and projects.

## 3. The traps (read before calling anything a regression)

- **A mix shift is read before any delta** (above).
- **`—` is below a floor, never zero.** A share needs `n ≥ 20`; a mean needs a
  non-zero `n`. A KPI with `—` on either side has no delta and is not evidence.
  Do not compute one from the JSON.
- **`agentSwitched` and `modelSwitched` misses are explained, not waste.**
  `unexplainedWasteShare` already subtracts them. A rise in raw `wastedUsd`
  with flat unexplained waste is users switching agents or models.
- **An `estimated` row is attribution, not measurement** (`estimated_rows`): a
  stopped turn's tokens were estimated. Many of them on one side means the
  cost delta carries an estimate.
- **A thumbs arm under the floor says nothing** (`thumbs_below_floor`). At
  prod's rate it sits there most of a version's life; it is the last row on
  purpose.
- **A cost delta on a deploy day is a boundary, not a change**
  (`deploy_in_window`, `no_full_day`): both builds wrote rows that day.
- **Latency is by time, not by build** (`latency_mixed_window`): a window with
  a deploy in it mixes builds' TTFT.
- **A TTFT delta under the probe's daily-median noise is "no evidence", not
  "unchanged".** Server first-token p50 moves a few hundred ms day to day on
  dev with no code change; a delta inside that is noise either way.
- **`commit_attributed`** rows were labelled by the deploy-time map, so a
  build there is a commit, not an image hash: fine for a version readout,
  weak for a build readout.
- **`payload_not_measured`**: tools whose rows predate the census have calls
  and no payload; the table prints `—`, and the version-level
  `toolPayloadPerCall` on such a window reads low, not zero-cost.
- **`unmetered_rows`**: calls on a model with no catalog row priced to $0, so
  cost per turn on that side is understated. That is a catalog fix first.

## 4. Flag regressions, then say where to look

Flag a KPI only when it got worse, both sides are over the floor, and no
caveat explains it (mix shift, deploy day, estimated rows). For each one,
name the next place to look:

| KPI moved | Look at |
|---|---|
| `unexplainedWasteShare`, `partialMissShare`, `cacheEfficiency` down | The candidate's costliest sessions (below), then `GET /admin/costs/sessions/{id}/calls` for each: find a `miss_avoidable` or `partial_miss` row and diff its `prefixFingerprints` (`toolConfigHash` / `systemPromptHash` / `historyHash`, nested in that object, not top-level fields) with the call before it. The hash that flipped names the cache-buster (CLAUDE.md, prompt-cache contract). Rule out `agentSwitched` / `modelSwitched` on the row first |
| `costPerTurn` (per model, no mix shift) | Cost per call vs calls per turn: more calls per turn is the agent looping (tool errors, `meanTurnDurationMs`); a higher cost per call is a bigger prefix or payload (`toolPayloadPerCall`, `documentTurnShare`) |
| `toolPayloadPerCall`, a tool's `resultCharsPerCall` or `estInputUsd` | The tools table in `readout.json`: which tool's chars per call moved. Then the change to that tool's result shape in the release's diff (`git log <baseline>..<candidate> -- backend/src/agents/main_agent/tools/`) |
| `toolErrorRate`, a tool's `errorRate` | The tool's line in `toolCensus` on the candidate's costliest sessions (`GET /admin/costs/sessions/{id}/profile`: calls, errors and payload per tool), and the Runtime logs for that tool name |
| `emptyAnswerShare`, `interruptedShare` | The turn outcomes in `quality.outcomes`; then the stream path (`docs/specs/turn-path-ttft.md`) and the smoke rows |
| `meanFirstTokenMs`, server TTFT p50/p90 | The TurnLatency dashboard by `appVersion` in Logs Insights, and `docs/specs/turn-path-ttft.md` §6 for the stage that moved |
| An eval family `fail` or a smoke `FAIL` | `eval_harness.py score --explain` on that run, or the smoke run's own output |

**The candidate's costliest sessions.** `GET /admin/costs/top-sessions?period=YYYY-MM`
ranks the whole month, not one version, so its top rows can belong to the
baseline or to a build before either. Walk its list in order and keep a
session only when its `/calls` rows carry the candidate's `appVersion` (and
`build`, for a build readout); a session that spans a deploy has rows on both
sides, so read only the candidate's rows. Rows written before the version
stamps shipped have `appVersion: null` and belong to neither side.

Never paste a session id, user id, title or message into the readout or a PR
body. Ids you look up stay in the terminal.

## 5. Verdict

End with one word and the KPI that decided it:

- **ship** — nothing worse over the floor that the caveats don't explain.
- **watch** — a KPI moved the wrong way but the evidence is thin (a short
  window, a mix shift that could explain it, `n` just over the floor). Say
  what would settle it: more days, a build-vs-build readout, the next nightly.
- **investigate** — a KPI moved the wrong way, over the floor, unexplained by
  any caveat, or an eval gate failed. Name the first place to look from §4.

Example: "**watch** — `toolPayloadPerCall` +28% (417 / 47 calls), but the
candidate has one partial day and a Haiku 4.5 → 5.5 mix shift; re-run when
1.34.0 has three full days."

For a release PR, paste `readout.md` and the verdict line under a
**Ledger readout** heading.
