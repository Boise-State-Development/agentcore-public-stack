# Conversation usage patterns

**Status:** collecting data. Levers are identified and priced but deliberately parked: build on a few more runs of evidence before spending engineering time at the margins.
**Baseline:** production, 30 days ending 2026-10-09: $2,287 across 7,503 conversations and 64,436 model calls.
**Tooling:** the `/usage-patterns-dashboard` skill (`.claude/skills/usage-patterns-dashboard/`) reproduces every number here and refreshes a dashboard with run-over-run history. The rules are in `scripts/build_datasets.py` and are described in `references/taxonomy.md`.

## Why patterns

Earlier audits named cost *mechanisms*: partial prefix misses, the compaction spiral, the hourly system-prompt tick. The per-conversation diagnoses in `admin/costs/diagnoses.py` encode those. They answer why one conversation cost money, not which kinds of work carry the spend. A usage pattern names the workload, which is what a product or engineering change is designed around.

Each conversation gets one **pattern** (first matching rule wins) and any number of **modifiers**. Every rule reads content-free features: tool ids and counts, token counts, timings, model ids, upload type and size. No rule reads message text or titles.

## Baseline

| Pattern | Conversations | Spend | Share | Per conversation |
|---|---|---|---|---|
| Quick Q&A | 6,088 | $495 | 21.7% | $0.08 |
| Iterative drafting | 110 | $298 | 13.0% | $2.71 |
| Long-form chat | 295 | $266 | 11.6% | $0.90 |
| Unclassified | 246 | $262 | 11.5% | $1.06 |
| Document study | 233 | $189 | 8.3% | $0.81 |
| Browser crawl | 95 | $186 | 8.1% | $1.96 |
| Web research | 212 | $165 | 7.2% | $0.78 |
| Batch items | 42 | $126 | 5.5% | $2.99 |
| API pipeline | 22 | $105 | 4.6% | $4.76 |
| Workspace assembly | 24 | $83 | 3.6% | $3.46 |
| Pasted corpus | 99 | $61 | 2.7% | $0.61 |
| Spreadsheet loop, screenshot support, Office generation | 37 | $51 | 2.3% | — |

Modifiers by share of spend: over 100K context 48%, slow cadence 30%, tool loop 16%, premium model 15%, multi-week 11%, large paste 8%.

Spend by mechanism: idle re-write (cache writes after a pause of 5+ minutes) $640 (28%), output $566, cache read $348, growth write $313, first write $198, live re-write $72, uncached input $44, unattributed (API rows) $105.

## What the data says so far

1. **The 5-minute cliff is the dominant cost.** Under 5 minutes, 3–6% of calls re-write more than they read; from 5 to 15 minutes, 57% do. That bucket alone is $325 of cache writes.
2. **Most idle re-writes are on ordinary-sized conversations.** $406 of the $640 lands on 25–100K-token contexts, below the 100K compaction ceiling; only $49 is for tokens above 100K. Compacting harder would barely help and is the one lever that costs context. The lever is making conversations smaller by removing content that is duplicated or can be fetched back.
3. **Iterative drafting is the costliest engineerable pattern.** `update_artifact` is full-replace, so every edit regenerates the document as output and leaves another full copy in history.
4. **Two blind spots.**
   - Large pastes go straight into the message body. The composer has no paste handling and nothing on the chat path enforces a maximum length, so the pasted text never reaches the document-offload path.
   - API-key traffic (`api-converse`) is missing from the console's conversation list and records cost without a breakdown.
5. **Compaction often can't reach its target.** 171 conversations ($680) logged `floor_unreachable`: the three protected turns (counted as user prompts, so a 24-call browsing turn is one) held more than the floor. When that tail alone exceeds the ceiling, hysteresis never re-arms and later cuts are all `forced`.
6. **Already working:** cross-conversation prefix sharing. 65% of new conversations read a cached tool and system prefix, median 13K tokens.

## Parked levers

Thirty-day estimates against the baseline. They overlap a little, so they are not strictly additive. The quality bar for all of them: no lost context, so the content either stays or is one tool call away.

| Lever | Est. 30-day | Quality | Where it lives |
|---|---|---|---|
| `edit_artifact` (patch edits) + `read_artifact` | part of $58 update output | Neutral | `agents/builtin_tools/artifacts/`; needs `s3:GetObject` on the artifacts bucket for inference-api; gate at `stream_coordinator.py` artifact SSE |
| Stub superseded artifact bodies at free moments | ~$82 | Neutral with `read_artifact` | Beside `session/document_offload.py`, using `_document_offload_reason` |
| Age old tool results on warm agents to the offload store, instead of today's cold-restore-only 500-char truncation | ~$35 | Positive: consistent, recoverable | `turn_based_session_manager.py` head-of-turn path; `core/tool_result_offload.py` storage |
| Opt-in batch mode (fresh context per item) | up to $58 | Needs a product decision; some batches compare items | SPA + session model |
| Large paste becomes a `.txt` attachment | ~$30 (mid-conversation pastes) | Same as attachments; run the document-offload quality check first | SPA `chat-input` paste handler |
| `analyze_spreadsheet`: align the docstring with the AST policy (`openpyxl` advertised but forbidden) and return the specific policy error | ~$5 | Positive | `builtin_tools/spreadsheet_analysis/analyze_tool.py` |
| `api-converse`: record the cost breakdown and a session row; add a cache point | visibility; caching at most ~$20 | Neutral | `app_api/chat/converse_routes.py` |

**Declined for now:**
- **Browsing sub-agent:** ~$28. The cost is loop length (results are already capped at 8K chars), and a sub-agent adds latency and summary loss.
- **1-hour static-prefix TTL:** about break-even on this data. The flag is not wired into CDK.
- **Lower compaction threshold:** costs context.

## Data to collect next

Gaps where the classifier has to estimate. Each is a content-free field on the `C#` cost row or the session row:

- **User-turn boundary on each call.** Turns are inferred from 60-second gaps today; `messageCount` deltas are noisy around compaction.
- **Tool-result tokens per tool.** The census has calls and errors but not size, so tool-result growth is inferred from context deltas.
- **Artifact size per write.** The artifact's bytes per version, to price superseded copies and patch-edit savings without using output tokens as a proxy.
- **Pasted characters on the user message.** A count only, to replace the paste heuristic.
- **Warm vs restored agent per turn.** This decides whether tool-result aging already happened on that turn.
- **Census accuracy.** Don't count the OAuth 401 retry as a tool error; it inflates `search_files`' 58%.

Process:
- Re-run the skill weekly and watch the discovery queue (unclassified conversations over $1, 66 at baseline).
- Promote a signature to a pattern once five or more conversations share it, as with pasted corpus.
- Bump `RULES_VERSION` with every rule change so share shifts from rule changes stay distinguishable from real changes in usage.
- Revisit the parked levers once the history shows which patterns are growing.
