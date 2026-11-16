# Idle returns: compaction, cache TTL, and a recoverable original

**Status:** design only. No application code until the developer agrees.
**Follows:** [`idle-return-context.md`](idle-return-context.md) (PR 2039) and Run 2 of [`conversation-usage-patterns.md`](conversation-usage-patterns.md).
**Data:** production `C#` cost rows from 2026-10-17 to 2026-11-14: 77,051 calls and $3,231. Content-free fields only: `tokenUsage`, `cost`, `cacheStatus`, `cacheGapSeconds`, `compactionEvents`, `turnDurationMs`, model id, and `toolCalls` names. Nothing here names a conversation or a user.

**Bottom line:**

- **Go: compact at the idle return, but only with the original kept recoverable.** The cut is decided after a turn ends and applied only when the next call is cold. Replayed, it nets **+$200–300 over 28 days**, and it adds nothing before the first token. The Haiku 5.5 screen (below) shows the cut alone loses real recall: 22/36 recalls against 36/36 for the control. So the recoverable original is a requirement, not an option.
- **No-go: a 1-hour TTL, in every form we replayed.** Combining it with compaction makes compaction worse, not better.
- **Go: the notice.** It is an additive change to the `compaction` event and is safe for both clients.

## What the code already does

The machinery for H1 exists. It runs only above the 100K ceiling.

- `update_after_turn` decides a cut after the model has answered (`stream_coordinator.py`, between the final `metadata` and `done`). That moment delays `done`, not TTFT.
- The summary comes from AgentCore Memory's own `ConversationSummary` records. Nova 2 Lite bounds it only when it runs over the 8K budget. It is never the turn's model.
- With deferred apply on (the default), the cut is parked as `pending_checkpoint`. `apply_pending_compaction` promotes it at the head of the next turn only on `cache_expired`, `prefix_changed` or `hard_ceiling`. That step is in-process work: 4 ms warm, 56 ms cold, all of it the existing DynamoDB re-read. It runs on warm and restored agents alike.
- In prod, 72% of applied cuts already land on calls after a gap of more than 5 minutes.
- Summarized messages leave only the list sent to the model. The AgentCore Memory events stay, and `GET /messages` still returns the full history.
- **No tool can read summarized turns back.** `retrieve_offloaded_content` covers tool results offloaded when they arrive. `document_read` covers uploads and, since #2066/#2079, compacted images.

So H1 is not a new mechanism. It is a second, lower trigger on the existing deferred path, restricted to cold calls.

## H2: why 1 hour doesn't pay, with or without compaction

**The break-even ignores context size.** At 1h, every written token costs 0.75× the base rate extra (2× against 1.25×). It earns 1.15× (1.25 − 0.1) each time it survives a return that comes 5–60 minutes later and would otherwise have been re-written. So a 1h write pays only when:

> expected number of 5–60-minute returns that token survives ≥ 0.75 / 1.15 ≈ **0.65**

The conversation's size cancels out. Shrinking the context with compaction cannot flip the sign by itself. What compaction does change is the mix, and it changes it the wrong way:

- It shrinks the re-written context at returns, which is where the 1h benefit comes from.
- It leaves new growth untouched, and the 1h premium is paid on all growth.
- A 1h TTL turns 5–60-minute returns into hits. That removes the free cut moment on exactly those returns: cuts fall from about 1,075 to 600–750.

**Measured gaps after an idle turn end.** An idle turn end is a turn followed by a gap of more than 5 minutes, or by nothing.

| Gap | 5–15 min | 15–60 min | 1–4 h | > 4 h | Never in window |
|---|---:|---:|---:|---:|---:|
| Share of idle ends | 27% | 13% | 5% | 8% | 47% |

Nearly half of the idle ends never return, and they pay the premium for nothing. Replayed per conversation, the 1h-all arm, bucketed by how many returns each conversation had after the fact:

| Returns in the conversation | 0 | 1 | 2 | 3–5 | 6–10 | 11+ |
|---|---:|---:|---:|---:|---:|---:|
| 1h net, 28 days | −$271 | −$62 | −$15 | −$15 | +$27 | +$55 |

Only conversations with 6 or more returns clear 0.65. An ex-ante rule, "1h once this conversation has had k returns", captures about half of their +$82: **+$28 to +$37 for k = 2–10**.

**It still loses to compaction.** Added on top of compaction it subtracts $50–63, because the two levers compete for the same returns.

**The cold restore makes 1h worse still.** Of the 15–60-minute returns, 65% carry a byte-changing restore event (`truncation_anchor` or `document_rehydrated`), so the history misses whatever the TTL is. Every 1h row below assumes the anchor advance would be gated on the TTL ("anchor gated"). As coded today, the 1h-all arm is −$560.

## H3: what a recoverable original buys

The quality risk of a lossy cut is that the model can't recover a detail it dropped. Make the dropped turns retrievable through a tool the model already has.

- **Write the transcript at decision time.** When the return cut is decided after the turn, the turns it will drop are rendered as text and stored under the offload store's existing per-user, per-session prefix, `compaction-offload/{user}/{session}/`, with a deterministic key per checkpoint. Re-deciding the cut overwrites the same key. It is one S3 PUT, off the turn path. Rewound spans are excluded.
- **Put one line in the summary block:** "earlier turns: `retrieve_offloaded_content` reference *R*". `retrieve_offloaded_content` is already one stable spec in `toolConfig` whenever the offloader is on, so **`toolConfig` does not change**. The summary is persisted verbatim, so the prefix stays byte-stable.
- **A new `recall_earlier_turns` tool would cost more.** Registering it only after a cut would key a new agent build (a rebuild at the head of the turn). Registering it always adds its spec to every session's prefix.

**How often the model reaches back, and what it costs.** There is no direct measure, because no such tool exists yet. As a proxy, 4.9% of calls after a real cut use an existing reach-back tool, against 4.7% of all calls. One reach-back is one extra tool round: a bounded grep window of about 2K tokens, plus a re-read of the context from the warm cache. That is about $0.015 on Sonnet 5 at 40K. The cut saves about $0.25, so the model could reach back about 16 times per cut before the saving is gone. At 5–25% of the following turns, reach-backs cost $4–20 over 28 days.

## The table

All figures are input-side spend on Claude calls (base $2,457 over 28 days). Outputs are assumed unchanged, which holds only if quality holds; the eval below settles that. "Cut" means: at a cold call, if the context is at least *T*, cut to the existing 25K floor or to the protected three turns, whichever is larger, plus a 3.8K summary (the prod median). The protected-tail estimate is bracketed ×1–×2.3, calibrated against the `floor_unreachable` estimates recorded in prod.

| Policy | 28-day net | Quality risk | TTFT effect | Verdict |
|---|---:|---|---|---|
| (a) Today | $0 | — | — | baseline |
| (b) Cut at return, T = 40K, decided after the turn (trigger T3) | **+$268 to +$324**, less ≤$14 decision cost and $4–20 reach-back | Lossy, on about 1,100–1,260 cuts in 380–450 conversations. Screen without H3: 22/36 against 36/36, and every failure abstained. H3 arm not yet run | None new. The apply is the existing head-of-turn step. The decision delays `done`, not the first token | **Go**, behind an in-development flag, gated on the eval |
| (b) T = 60K / 80K | +$253–308 / +$212–262 | Fewer cuts (893–1,059 / 611–736) | Same | Fallback if the eval fails at 40K |
| (b) Decided concurrently with the return turn, applied next turn (T2) | +$10 to +$16 | Same as T3 | None | **No-go**: the second re-write eats the saving |
| (b) Background cut a few minutes after the last turn (T1) | ≈ T3, less the summaries for the 47% who never return | Same | None, but it needs a timer in an idle microVM | **No-go**: T3 gets the same saving without the timer |
| (c) 1h on every point | −$281 (as coded: −$560) | None | None | **No-go** |
| (c) 1h on turn-opening calls | −$202 | None | None | **No-go** |
| (c) 1h once a conversation has had 2, 5 or 10 returns | +$32 / +$37 / +$28 | None | None | **No-go**: small, and it loses to (b) |
| (c) 1h on the static prefix only (prior replay) | −$7 to +$8 | None | None | **No-go** (settled) |
| (d) Cut + 1h on every point | −$175 to −$190 | As (b) | As (b) | **No-go** |
| (d) Cut + 1h only after a cut | +$171 to +$196 | As (b) | As (b) | **No-go**: worse than (b) alone |
| (d) Cut + 1h after 5 returns | +$218 to +$261 | As (b) | As (b) | **No-go**: worse than (b) alone |

**Why this is bigger than the earlier $100.** `idle-return-context.md` sized only the re-write at the return itself. A cut made at the first return also shrinks every warm read after it and every later return. 54% of the spend sits in conversations with 6 or more returns, so the saving compounds. Per cut it is about $0.25.

**Caveats:**

- Turns are inferred from `turnDurationMs`, which a turn's last row carries.
- The static prefix is imputed (the smallest prompt, capped at 16K).
- Real cuts reset the replay's offset, so the overlap with today's compaction is not counted twice.
- A precomputed cut protects the three turns before the return, plus the return turn itself. The ×2.3 bracket covers that.

## Design of (b) + H3

**One policy, not a second trigger.** Today's 100K cut is already return compaction with a high trigger. Of the 1,732 cuts applied over 28 days, 1,266 (73%) landed on a cold call, because deferred apply parks a cut until the cache has expired. So the idle-return cut replaces the 100K trigger's cold path, and does not sit beside it. The 100K trigger is not simply removed, though:

- It is what bounds conversations that never pause.
- Warm calls with more than 100K of context cost $553 over the 28 days, 17% of spend.
- Without a bound, those conversations keep growing until a forced cut on a warm cache: the expensive kind of re-write, and the shape of the earlier compaction spiral.

The merged policy has two rules:

| When | What happens |
|---|---|
| The next call is cold and the context is at least `T_return` (model-relative, default 40K) | Cut. This takes over today's cold applies at 100K |
| The cache is warm and the context keeps growing | No cut until it nears the window, then one forced cut. This is the safety net, and the only path that ever pays for a warm re-write |

The replay's +$200–300 is already net of today's cuts (its offset resets at every real cut), so the merged policy keeps today's saving and adds this one.

1. **Decision (after the turn, beside today's `update_after_turn`).** If the turn's input is at least `T_return`, choose a checkpoint with today's `choose_checkpoint` (same floor, protected turns and tail bound).
   - Park it as `pending_checkpoint` with `pending_reason = "idle_return"`.
   - Fetch the summary as today, and write the transcript object (H3).
   - Re-decide on every qualifying turn end, replacing the parked one, so the protected tail is always the latest three turns.
   - This decision replaces today's ceiling decision for cold applies. The warm safety net keeps its own trigger near the window.
2. **Apply (head of turn, existing step).** An `idle_return` cut promotes **only** on `cache_expired` or `prefix_changed`. A warm continuation keeps reading the cache and never pays a second re-write. Only the safety net cuts on a warm cache.
3. **The cache contract.**
   - State lives on the session row and is re-read every turn (`_adopt_persisted_compaction_state`).
   - Every agent instance on the session applies the persisted cut and never decides its own. The checkpoint and anchor never move backwards.
   - The offload key is scoped to `(user, session)`.
   - The summary and its reference line are persisted verbatim, so every restore prepends identical bytes.
4. **Moving the decision after `done`.** Today it sits before `done`: an LTM list (100–300 ms), plus Nova 2 Lite when over budget. Moving it after `done` is a separate, measured change. The SPA clears loading on close, not on `done`, so the measurement is close-to-idle time on dev.
5. **Open checks before building:**
   - How rewind and fork treat a pending `idle_return` cut and its transcript object.
   - Whether session delete cascades the `compaction-offload/` prefix.
   - Whether the offloader's storage format accepts an object we write ourselves, rather than one from the plugin's intake hook.

## The notice

Today the `compaction` event fires when a cut is **decided**. With deferred apply that is before the model ever sees the summary, and the event carries no reason. The change:

- **Emit it on the turn where the cut took effect,** after that turn's `metadata`, so frame order I1 (`metadata` → `compaction?` → `done`) is unchanged. Don't emit it for an `idle_return` decision, which may never apply.
- **Add optional fields:**
  - `reason`: `idle_return`, `context_limit`, `forced` or `prefix_changed`
  - `idleSeconds`
  - `tokensBefore`: the previous call's measured input
  - `tokensAfter`: this turn's first call, measured, not estimated
  - `tokensSaved`
  - `recoverable`: true when the H3 transcript exists
- **Both parsers ignore unknown keys.** The SPA's `validateCompactionEvent` requires only the existing four numeric fields. The TUI's `agent_events.py` reads the same four through `_as_int`. The change is additive.
- **Persist it.** Lift the latest `{reason, tokensSaved, idleSeconds}` onto `SessionMetadata` next to `totalSummarizedTurns`, so a reload shows it. The event itself is never replayed.
- **SPA.** `CompactionSummaryComponent` takes the reason and the tokens. For an idle return: "You were away, so 12 earlier turns were summarized (about 38K tokens). The assistant can still look them up." For a context limit, the current copy plus the token figure.
- **TUI.** `CompactionNotice` exists but is never instantiated. Mount it with the same text. This is optional and separate.

## Settling the quality trade

### The screen that ran (Haiku 5.5, 2026-11-14)

**What it is.** A new opt-in, advisory eval family, `idle_return_recall` (`backend/evals/cases/idle_return_recall.json`), with 12 cases. In each case:

- Turn 1 opens with a background line, and the fact sits on the line after it: a reference code, a corrected budget, a name, a moved date, a decision and its reason, a quantity, a standing instruction, a position in a list, a cost centre, a room, a deadline, or a replaced contact.
- Four unrelated turns follow, with a pause past the TTL before turn 3 and another before the question.
- Answers that use lookalike values are scored as failures.

**The two arms** ran on a local inference stack from this branch, against dev:

- **Control (`default`):** no cut.
- **Cut (`idle_return` ceiling, capped at 7.3K):**
  - Every one of the 36 samples decided a cut (`checkpoint 0 → 4`, summarizing turns 1–2) after a turn.
  - All 36 cuts were applied at the cold return with `reason=cache_expired`.
  - Today's code ran unchanged; the arm differs only by config.

| Arm | Pass (k=3) | Cost |
|---|---:|---:|
| Control, no cut | **36/36** | $0.13 |
| Cut at the cold return, no recoverable original | **22/36** (61%) | $0.12 |

The whole screen cost about $0.31. That includes one probe sample and a first attempt that was discarded (below). It is well under the $3 estimate, because these conversations are 7–9K tokens, not 40K.

**What it shows:**

- **Recall survives the cut only when the model's own acknowledgment repeated the fact.** Five facts passed 3/3 under the cut (code, corrected budget, moved date, quantity, replaced contact). The model had echoed each of them in its turn-1 reply. The fallback summary keeps the first 150 characters of each message's first line, so that echo carried the fact through. A room, a list position, a deadline and a site lead's name were mostly lost.
- **No sample hallucinated.** All 14 failures abstain: "the summary cuts off before…", and usually "please resend it". That is the best failure mode, and it is exactly the gap a `retrieve_offloaded_content` reference closes. The model already knows something is missing and asks for it.
- **Every cut here used the fallback summary, not AWS Memory's.** No `Retrieved N summaries from LTM` lines appeared, even with the cut decided more than 5 minutes after turn 1. In prod the fallback is rarer: across the 729 sessions that record a summary source, 89% of the latest cuts used AWS Memory summaries (472 updated since 2026-10-17). A cut at 40K lands in younger conversations than today's 100K cuts, though, so its fallback share will be higher. **This screen measures the fallback regime: the floor, not the typical case.**
- **The low cap first tripped document offload instead.** At a 14.8K cap, every sample crossing the ceiling offloaded the attached document on a warm turn. The context fell below the cap and no conversation cut ever happened. The run was discarded, and the cases were rebuilt without an attachment. At a 40K trigger the same interaction applies, so the design must keep a return cut from triggering a warm document offload.

### What still has to run

- **A third arm: cut with the H3 reference line.** This needs the application code. The gate is that cut-with-H3 stays within noise of the control (36/36 here) at k=3.
- **A Sonnet 5 confirmation at 40–60K, estimated $35–80.** Run it after H3 exists. Ask before running it.
- **Re-running `attachment_tools.*_after_compaction` on the H3 arm.**

To reproduce:

1. Start two local inference stacks with the dev Runtime's environment, one with `AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS=7300`.
2. Run each arm:

```bash
eval_harness.py run --family idle_return_recall --ceiling {default|idle_return} --invocations-url … --k 3
```

## Attribution: knowing in prod whether it paid

**Dashboards can't judge it.** In the four weeks before any change, idle-return spend moved like this:

| Week from 2026-10-17 | Total spend | Idle-return spend | Share | $ per return |
|---|---:|---:|---:|---:|
| 1 | $761 | $176 | 23% | $0.18 |
| 2 | $831 | $219 | 26% | $0.21 |
| 3 | $834 | $281 | 34% | $0.23 |
| 4 | $785 | $242 | 31% | $0.21 |

That is a $105-a-week swing in idle-return spend, with nothing shipped to change it. The expected effect is about $50–75 a week. Launched into a week like week 3, the feature would look like a failure. Launched into a week like week 1, it would look like a success, whether it worked or not. The release ledger has the same problem: a release carries everything else in it too.

**The quality cost is invisible without a comparison.** In the screen, the failure mode was the model saying "I don't have that, please resend it". In prod, each such failure costs a user turn and the user's time. No cost line attributes that turn to the cut.

So the instrumentation is part of the feature's definition of done. It is a few fields on rows already written after the model answers, plus a hash split, and nothing on the turn path:

1. **Tag the cut.**
   - Add `reason` (`idle_return` or `context_limit`), `tokensBefore` and `tokensAfter` to the `applied` ledger event. Today the event has no reason, so an idle-return cut cannot be told apart from today's cold applies.
   - These are the same numbers the notice needs, so it is one change.
2. **Tag the reach-back.**
   - Today a cost row records only tool names and call counts, so a reach-back into the stored turns looks like a `retrieve_offloaded_content` call for an offloaded tool result. Separate them with a distinct reference prefix, or with a counter on the row.
   - Reach-backs are the cost a cut pays back. The summary call is a small side-channel cost (Nova 2 Lite, about $0.001 a decision) and stays unmetered.
3. **Hold out a comparison group.**
   - Split by a deterministic hash of the session id, and stamp each cost row `treated` or `holdout`.
   - Run 50/50 for the first two weeks in prod, while the flag is still in-development.
   - The replay predicts about 1,100 cuts across 380–450 conversations per 28 days. Cost per conversation is very uneven (the top 1% of conversations carry 19% of idle-return spend), so a 10% holdout (about 40 conversations with cuts a month) cannot put a confidence interval on dollars.

**Reading it.**

- **Dollars come from per-cut accounting.** Take the tokens each tagged cut removed (`tokensBefore − tokensAfter`) and price them on every later call in the conversation until the next cut, at that call's read and write rates. Then subtract the tagged reach-backs.
- **The holdout gives the sign and the quality.** Compare cost per conversation that had a return, resend-style turns, thumbs-down, and the reach-back rate.

**Exit rule.** The flag stays in-development until two weeks of holdout show both:

- the per-cut saving holding up
- no rise in resend-style turns

If either fails, the cut is withdrawn, and the recoverable original stays only if it pays for itself elsewhere.

## Recommendation

| Lever | Verdict |
|---|---|
| Compaction at the idle return (decided after the turn, applied only cold, T = 40K) | **Go only together with the recoverable original**, behind an in-development flag. Alone it scored 22/36 against 36/36 |
| Recoverable original through `retrieve_offloaded_content` | **Go**, as part of the same change. The screen shows it is required: every failure was an abstention it would answer |
| Summary source at decision time | **Investigate**: the fallback (first 150 characters per message) is what loses the facts. Re-reading AWS Memory summaries at apply time would sit on the TTFT path, so don't. Prefer a better post-turn summary |
| 1h TTL (all, turn-open, per-conversation, after a cut, static-only) | **No-go** |
| Concurrent or background compaction triggers | **No-go** |
| Notice with reason and tokens saved | **Go**, independent of the rest; additive fields |
| Replace the 100K trigger's cold path with the idle-return cut; keep a warm safety net near the window | **Go**, as part of the same change. Removing the 100K trigger outright would leave conversations that never pause ($553 per 28 days of warm calls over 100K) to grow into warm forced cuts |
| Attribution: tagged cut, tagged reach-back, 50/50 holdout for two weeks | **Required** before the flag leaves in-development. Weekly idle-return spend swings more than the expected effect |
