# What a returning user re-writes

**Status:** exploration, no code. Candidate 3 of the [2026-11-13 efficiency review](../efficiency-reviews/2026-11-13.md).
**Data:** production `C#` cost rows, 2026-10-16 to 2026-11-13 (the scan includes the morning of the 13th). Content-free fields only: `cacheStatus`, `cacheGapSeconds`, `tokenUsage`, `cost`, `toolCalls` names and sizes, `documentTokens`, `compactionEvents`, model id. Nothing here names a conversation or a user.
**Bottom line:** of the $940 that calls after the 5-minute cache expiry cost, roughly **$30–90 can be removed without losing context**. The rest is the static prefix, the conversation's own text, and attachments the user is still working on. Re-size candidate 3 to that range, and build the large-paste lever first.

## The $940

4,501 calls carry `miss_ttl_expired`: 28% of the window's $3,369. Of that, $834 is cache writes, $102 is output and $4 is input. **No return read any cached prefix**, not even the shared tools and system prefix, so every return re-writes the whole prompt.

| Context at the return | Calls | Cost | Share |
|---|---:|---:|---:|
| under 25K | 921 | $53 | 6% |
| 25–50K | 1,374 | $162 | 17% |
| 50–100K | 1,460 | $349 | 37% |
| 100–150K | 525 | $211 | 22% |
| over 150K | 221 | $164 | 17% |

The median return is 49K tokens. 76% of the cost is below 150K, so compacting harder is still not the lever (Run 2 found the same thing).

| Gap since the last call | Calls | Cost | Share |
|---|---:|---:|---:|
| 5–15 min | 2,166 | $482 | 51% |
| 15–60 min | 1,089 | $224 | 24% |
| 1–4 h | 428 | $81 | 9% |
| 4–24 h | 398 | $76 | 8% |
| over 1 day | 420 | $76 | 8% |

**The 15-minute line matters more than the 5-minute one.** The Runtime keeps a microVM for `idleRuntimeSessionTimeout`, 900 s by default, and we don't override it. Under 15 minutes the agent is still warm. `initialize()` does not run again, so the restore-time transformations don't run either: the truncation-anchor advance (old tool content cut to 500 characters) and document rehydration (inline bytes swapped for a digest). Only 43 of the 2,166 warm-band returns carry a `truncation_anchor` event, against 1,440 of the 2,335 cold ones. **Half the spend lands on the path where the only thing that runs is `apply_document_offload`.** This settles the "warm vs restored agent" gap in the one-pager's data list: the gap length tells them apart.

By model: Sonnet 5 $566 (60%), Opus 5.5 $349 (37%), everything else $25.

**Concentration.** 1,671 conversations had a return. The top 1% (16 conversations) carry 19% of the spend, the top 5% (83) carry 43%, and the top 10% carry 58%. What drives it is repeated returns:

| Returns per conversation | Conversations | Cost | Share |
|---|---:|---:|---:|
| 1 | 848 | $121 | 13% |
| 2 | 344 | $113 | 12% |
| 3–5 | 279 | $194 | 21% |
| 6–10 | 154 | $254 | 27% |
| 11+ | 46 | $258 | 27% |

The top 5% average 11.8 returns each at a mean context of 102K, and 71% of them span more than one day. These are working documents people come back to, which is the product working as intended. A removal made once at the first return pays off at every later return.

## What sits in the context at a return

There is no per-block size on the cost row, so this is reconstructed from how each conversation's prompt grew from call to call. Two attribution methods were used, and they disagree by 2–4× on the small slices, so the small slices are given as ranges. Shares are of the $834 in writes.

| Slice | Share | Write $ | Can it go without losing context? |
|---|---:|---:|---|
| Static prefix: system + tools (`prefixTokens` median 10K, p90 27K) | ~24% | ~$190 | No. It is the agent |
| Rest of the opening context: first prompt, project or agent context | ~15% | ~$130 | No |
| Model output kept in history (answers and tool inputs) | 18% | $154 | No, except superseded artifact bodies (below) |
| Attachments | 13% | $117 | Mostly no; see below |
| User turns that added ≥8K tokens with no attachment growth ("paste-like") | 12% | $97 | Partly: $16–74 is re-written at returns *after* the paste's own turn |
| Tool-round input (tool results, screenshots, page slices) | 12% | $111 | Partly: $9–41 sits outside the three protected turns at warm returns (text-only census: ~$5) |
| Ordinary user prompts | 6% | $43 | No |

Attachments, clipped (see the measurement note below):

| Attachments at a return | Write $ |
|---|---:|
| Attached on the returning turn itself (new input) | $50 |
| Attached the turn before (pinned, the active subject) | $40 |
| Two or more turns old, at least 5K, still inline | $8 |
| Under the 5K offload floor | $2 |

**Artifacts are small here.** Census rows (from 2026-11-06) put all artifact bodies at 0.8% of return context and superseded ones at 0.4%. A cold restore already truncates every `toolUse` input over 500 characters outside the protected tail, and that includes artifact bodies.

## The levers, re-sized to returns

28-day estimates against this window, for return calls and the cache reads that follow them until the next return. They overlap a little.

| Lever | Est. 28-day | Quality | Cache contract | TTFT |
|---|---|---|---|---|
| **Large paste becomes a `.txt` attachment** | **$15–55** | As attachments: inline on its turn (documents-as-text on Converse), then a digest one `document_read` away | Clean. Offload and rehydration already keep the prefix byte-stable | None on the server: the upload happens at paste time, and the turn takes the existing attachment path |
| Warm-return parity: advance the truncation anchor at a warm `cache_expired` head of turn, so a 6-minute pause ends like a 16-minute one | $5–15 | Lossy: 500 characters, the same as a cold restore today. The offload-to-store variant would be recoverable | Needs care: persist the anchor before use (existing), and every agent on the session must cut its live list to the persisted anchor, not just its own decision | ~1–3 ms in-process (stdlib bench of the same deep copy and scan, 240–480 messages up to ~200K tokens), on ~4% of turns. No network. Offloading to S3 instead would put a PUT on the path, so don't |
| Old documents still inline at a warm return | ~$8 | Neutral | Existing path | None |
| Stub superseded artifact bodies + `read_artifact` | $3–11 | Neutral with `read_artifact` | Existing offload pattern | None |
| 1-hour TTL on the static prefix only (built, flag off) | −$7 to +$8 | Unchanged | Allowed: TTLs are non-increasing | None |
| Opt-in batch mode | not sized here | Needs a product decision | — | — |
| Return-aware compaction: a lower floor at a cold return for conversations with 6+ returns | ~$100 if it cuts a third of retained history | **A trade**: loses context | Clean at a cold return | None |

What changed from the one-pager's parked table. Its $82 and $35 were 30-day figures over all cached traffic. Over returns alone the artifact lever is a few dollars, because cold restores already cut the bodies, and the tool-result lever is small, because big results go to the offload store when they arrive and most of the rest sits in the protected tail. The 1-hour static TTL replays at about zero again, so stop carrying it.

**A quality finding comes out of the artifact line.** Restore truncation cuts the *current* artifact's body to 500 characters once it is older than three turns. A user who comes back after 15 minutes to edit it is working from 500 characters of their own document. `read_artifact` is worth building to fix that, not to save money.

## Recommendation

**Build first: a large paste becomes a `.txt` attachment.** Expected saving over returns is $15–55 in 28 days, central ~$35. The one-pager's $30 for mid-conversation pastes on warm reads comes on top, overlapping a little. It is the largest lossless slice, it needs no turn-path change, and it reuses machinery that already holds the prefix stable: documents-as-text on the attach turn, `apply_document_offload` at a warm return, and rehydration at a cold one.

- **Quality risk:** moderate and testable. The text reaches the model the same way on its own turn. After that it is a digest plus `document_read`, so a later question about the paste costs one tool call.
- **Eval:** the `document_qa` and `attachment_tools` families on the nightly track, plus one new case: paste ~20K tokens, ask about it, wait past the TTL, then ask about a detail the digest doesn't hold. The gate is the outcome pass rate holding. The bench is input tokens per passing case on the return turn.
- **TTFT:** none on the server. The upload happens when the user pastes, not when they send. Turns with attachments already pay for `_resolve_turn_attachments`. Measure it on dev for a paste followed by an immediate send, and state the number in the PR.
- **Before building, measure.** "Paste-like" means ≥8K tokens added on a user turn with no attachment growth. That also matches injected context that isn't a paste, which is why the range is $15–55. A content-free `pastedChars` count on the user message, already listed in the one-pager's data gaps, settles it in a week, and the SPA knows the length at paste time.

**Second, small and backend-only:** warm-return parity, after the paste lever. It is a consistency fix more than a saving.

**Re-size candidate 3** in the next efficiency review from "$939" to "about $30–90 addressable". The rest is human pace on the context the user is actually using.

## Measurement notes

- **`documentTokens` overstates.** On 28% of return calls that carry documents it exceeds the whole prompt, by up to 65×. Every document figure above is clipped to the prompt minus 10K. Sizing a document lever on the raw field would put documents at 37% of return context; clipped, they are about 10–13%. Worth an issue: the estimate likely counts blocks that the projection sends as text, or that the five-document cap drops.
- `prefixTokens` is on only 777 rows in the window, so the static prefix is imputed from each conversation's smallest prompt and capped at 16K.
- Turn boundaries are inferred from a gap of more than 60 seconds between calls. A long tool loop can split one turn into two, which makes "two or more turns old" an upper bound.

To reproduce: scan with the efficiency-review projection, plus `cacheGapSeconds` and `documentTokens`. Then replay each conversation's calls in order and attribute each prompt's growth to the previous call's output, attachment growth, a new user turn, or a tool round.
