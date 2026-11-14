# Idle returns: compaction, cache TTL, and a recoverable original

**Status:** design only. No application code until the developer agrees.
**Follows:** [`idle-return-context.md`](idle-return-context.md) (PR 2039) and Run 2 of [`conversation-usage-patterns.md`](conversation-usage-patterns.md).
**Data:** production `C#` cost rows from 2026-10-17 to 2026-11-14: 77,051 calls and $3,231. Content-free fields only: `tokenUsage`, `cost`, `cacheStatus`, `cacheGapSeconds`, `compactionEvents`, `turnDurationMs`, model id, and `toolCalls` names. Nothing here names a conversation or a user.

**Bottom line:**

- **Go: compact at the idle return, with the original kept recoverable.** The cut is decided after a turn ends and applied only when the next call is cold. Replayed, it nets **+$200–300 over 28 days**, and it adds nothing before the first token.
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
| (b) Cut at return, T = 40K, decided after the turn (trigger T3) | **+$268 to +$324**, less ≤$14 decision cost and $4–20 reach-back | Lossy, on about 1,100–1,260 cuts in 380–450 conversations. Mostly recovered with the H3 original | None new. The apply is the existing head-of-turn step. The decision delays `done`, not the first token | **Go**, behind an in-development flag, gated on the eval |
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

1. **Decision (after the turn, beside today's `update_after_turn`).** If the turn's input is at least `T_return` (model-relative, default 40K) and no regular cut is pending, choose a checkpoint with today's `choose_checkpoint` (same floor, protected turns and tail bound). Park it as `pending_checkpoint` with `pending_reason = "idle_return"`. Fetch the summary as today, and write the transcript object (H3). Re-decide on every qualifying turn end, replacing the parked one, so the protected tail is always the latest three turns.
2. **Apply (head of turn, existing step).** An `idle_return` cut promotes **only** on `cache_expired` or `prefix_changed`, never on `hard_ceiling`. A warm continuation keeps reading the cache and never pays a second re-write.
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

- **New eval family `idle_return_recall`, about 12 cases.**
  - Each case runs 6–8 turns. It plants facts in turns 1–3 (a number, a name, a decision with its reason) and does unrelated work in turns 4–6.
  - It then forces a cold call with a 301-second wait (wall-clock only, no extra cost).
  - Finally, it asks about each planted fact.
- **Three arms:**
  - today (no return cut)
  - cut without the H3 line
  - cut with the H3 line
- **Re-run the existing `attachment_tools.*_after_compaction` cases on the cut-with-H3 arm.**
- **Gate.** The pass rate of cut-with-H3 stays within noise of no-cut at k=3, and cut-without-H3 shows how much H3 is worth. Bench the input tokens per passing case on the return turn.
- **Estimated cost (not run):**
  - Haiku 5.5 screen: about 650 turns, roughly **$3**.
  - Sonnet 5 confirmation: **$35–80**, at 40–60K context per turn.
- **We ask before running either.**

## Recommendation

| Lever | Verdict |
|---|---|
| Compaction at the idle return (decided after the turn, applied only cold, T = 40K) | **Go**, behind an in-development flag, after the eval |
| Recoverable original through `retrieve_offloaded_content` | **Go**, as part of the same change: it is what makes 40K defensible |
| 1h TTL (all, turn-open, per-conversation, after a cut, static-only) | **No-go** |
| Concurrent or background compaction triggers | **No-go** |
| Notice with reason and tokens saved | **Go**, independent of the rest; additive fields |
