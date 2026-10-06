# Conversation search

**Status:** DRAFT plan, written 2026-10-06 against `develop` @ `6f40bbe1`. Nothing built yet.
**Tracking issue:** #1380 ("Enh: Search" — a user asks for ChatGPT-style search over conversations, artifacts and agents)
**Flags (in development, default OFF):** `CONVERSATION_SEARCH_ENABLED` / `CDK_CONVERSATION_SEARCH_ENABLED` / `features.conversationSearch`. One flag; the SPA title filter (PR-1) ships without it because it costs nothing and touches no API.
**Related:** `docs/specs/memory-baseline-decision.md` (summary records census, retrieval score calibration), `docs/specs/session-metadata-static-sort-key.md` (row shape, GSIs), `docs/kaizen/review-queue.md` §"sidebar conversation search".

## 0. Read this first: what the repo already has

The cheapest search index is one that is already paid for. Three facts decide this plan.

1. **Message text lives only in AgentCore Memory short-term events, and they expire after 90 days.** There is no DynamoDB or S3 copy of a transcript (`apis/shared/sessions/messages.py:359-569` reads `list_events`; `infrastructure/lib/constructs/agentcore/memory-construct.ts:74` sets `eventExpiryDuration: 90`). The session row (`USER#{uid}` / `S#{sid}`) has no TTL, so a 4-month-old conversation is a title with no messages behind it. Any index built from message text can only ever cover the last 90 days unless we start copying text, which is a retention decision, not a search one.
2. **Every session already has an LLM-written summary, maintained by AgentCore for free-to-us.** The memory has a `summaryMemoryStrategy` (`memory-construct.ts:78-97`). Prod holds **44,627 summary records** across 11,286 sessions, median 1,409 characters, in XML `<topic>` chunks (`memory-baseline-decision.md:276-282`). They live under `/strategies/{summaryId}/actors/{userId}/sessions/{sessionId}`, they are **not** subject to the 90-day event expiry, and session delete already purges them (`SessionService.delete_agentcore_memory`, `session_service.py:298-458`). We pay $0.75 per 1,000 records-month for them today whether or not anyone searches.
3. **`RetrieveMemoryRecords` matches `namespace` as a prefix** ("Searches for memory records in namespaces that start with the provided prefix", API reference). So one call with `namespace=/strategies/{summaryId}/actors/{userId}/` is a semantic search over **all** of a user's session summaries, scoped to that user by construction. It costs $0.50 per 1,000 calls and shares the account's 30/s quota with per-turn fact retrieval. The app-api already wraps this client (`apis/app_api/memory/services/memory_service.py`, `search_memories`), and `_get_strategy_namespaces()` already discovers the summary strategy id.

What does **not** exist: any search or filter over sessions, in the SPA or the API. The sidebar lists 30 sessions a page via `SessionRecencyIndex` (`session-list.ts`, `session.service.ts:16`); the list route takes only `limit` and `next_token` (`sessions/routes.py:64-119`). No `Cmd+K`, no palette, no `scrollToMessage` on assistant messages (only user messages carry `message-{id}` anchors).

## 1. What the user gets

A search box at the top of the conversation sidebar (and `Cmd/Ctrl+K` to focus it). Typing filters immediately; pressing Enter (or pausing) widens the search to every conversation the user owns, including ones the sidebar has not loaded.

Results replace the date-grouped list while a query is active. Each row shows the title, when the conversation last moved, and a one-line snippet of **why** it matched: the title itself, the opening prompt, or a sentence from the conversation's summary. Clicking opens `/s/:id` exactly as the sidebar does today. Escape clears. "No matching conversations" is distinct from "No conversations yet".

Two kinds of match, merged in one list:

| Match kind | Finds | Freshness | Cost per search |
|---|---|---|---|
| **Lexical** (title + opening prompt) | What the user remembers verbatim: a course code, a file name, a person's name they typed | Title lands seconds into the first turn | One DynamoDB query over the user's own session rows: fractions of a cent per thousand searches |
| **Semantic** (AgentCore session summaries) | What the conversation was *about*: "the one where we planned the syllabus rewrite" | AgentCore extracts about 70–100 s after a turn (`memory-baseline-decision.md:38`) | $0.0005 (one `RetrieveMemoryRecords`) |

Nothing in this plan touches the model call path, the system prompt, `toolConfig` or restored history. The cacheable prefix is unchanged. Time to first token is unchanged. The one optional item that does reach a prompt (an agent-facing tool, §7 PR-4) is called out separately and defaults off.

## 2. Options considered

| Option | Monthly cost at today's scale (1,800 MAU, ~3,800 session rows) | At 30k users | Verdict |
|---|---|---|---|
| **A. Client-side title filter** over loaded sidebar pages | $0 | $0 | Ship first (PR-1). Covers only pages the user has scrolled to; the kaizen review already flagged this. |
| **B. Server-side lexical** (`titleLower` + `firstPrompt` attrs, DynamoDB Query + FilterExpression on the user's partition) | ~$0.01 | < $1 | Ship (PR-2). No new infra. |
| **C. Semantic over existing AgentCore summaries** (`RetrieveMemoryRecords`, actor-prefix namespace) | ~$2 at 4,300 searches/mo | ~$35 | **Ship (PR-2). The core of the plan.** Zero indexing cost, zero new storage, delete-consistency already exists. |
| D. Own S3 Vectors index of turn text (Titan v2 embeddings, per-turn hook) | Embeddings ~$0.02/MTok → ~$0.50; storage $0.06/GB-mo → pennies; queries $2.50/M | ~$15 | Hold in reserve (PR-5). Cheap in dollars; expensive in surface: a second vector index, a per-turn write hook, user-id filterable metadata, a delete hook, a backfill that can only reach 90 days back, and verbatim snippets stored in vector metadata. Build only if §3's probe shows summary recall is poor, or if users need verbatim phrase search inside messages. |
| E. Per-user Bedrock Managed KB | $5/GB-mo + $1/1k retrieves | Hits the 1,000-KB account quota at ~1,000 users | Reject. |
| F. OpenSearch Serverless | ~$175–700/mo fixed floor | same | Reject: fixed cost dwarfs every other line here. |
| G. Our own Nova Micro "search card" per session | ~$0.0001/session | ~$10 | Reject as a duplicate of C. AgentCore already writes a better summary with a model we don't pay for. Revisit only for local-mode deployments with no AgentCore Memory. |

Pricing sources (checked 2026-10-06): AgentCore Memory long-term retrieval $0.50/1k, built-in strategy records $0.75/1k-month (aws.amazon.com/bedrock/agentcore/pricing); S3 Vectors $0.06/GB-mo storage, $0.20/GB PUT with a 128 KB minimum per PUT, $2.50/M queries (aws.amazon.com/s3/pricing); Titan Text Embeddings V2 $0.02/MTok; Bedrock Managed KB $5/GB-mo + $1/1k Retrieve.

**Why C beats D even though D's dollars are also small.** D adds a write on every turn, a second store that can disagree with the session table, and 90-day-capped coverage. C adds nothing to the write path and covers every session that has a summary, regardless of age. The remaining question is only whether summaries are *good enough* to find things by, which is what §3 settles before any UI is built on it.

## 3. Phase 0: prove the summaries are searchable (half a day, dev, read-only)

Extend `scripts/memory-audit/audit.py` with a `search` subcommand. For a handful of dev actors who have real history (the five heavy dev users with 100+ records are ideal):

1. **Coverage.** For each active session row of the actor (`SessionRecencyIndex`), does `ListMemoryRecords` on the session's summary namespace return at least one record? Report coverage overall and for sessions with `messageCount ≥ 2`. Expect gaps for: sessions forked from a share (`extractionMode="SKIP"`, `shares/service.py`), sessions created before the memory resource, and the 7 `LTM_RATE_EXCEEDED` failures the census found.
2. **Rank.** Author 10 queries per actor from their titles (paraphrased, never verbatim). Run `RetrieveMemoryRecords` with `namespace=/strategies/{summaryId}/actors/{actorId}/`, `topK=10`. Record the rank of the intended session and the score gap to the best wrong session.
3. **Snippet.** Check that returned records carry `namespaces` (to recover the session id) and that the `<topic>` XML can be flattened to a 200-character sentence.

**Gate:** coverage ≥ 80% of multi-turn sessions and the intended session in the top 3 for ≥ 80% of queries. Below that, PR-2 ships lexical only and PR-5 (S3 Vectors) moves up. Record the readout in this spec.

The earlier calibration (`memory-baseline-decision.md` §"Relevance cut calibration") found absolute scores sit in a narrow 0.34–0.67 band while *ranking* is good. Search should therefore rank by score and show the top N, never apply the runtime's 0.40 relevance cut: a cut tuned for "inject or stay silent" is wrong for "show the user a ranked list".

## 4. Data model changes

All on the existing session row. Nothing new in DynamoDB, no new GSI.

| Attribute | Written by | Size | Purpose |
|---|---|---|---|
| `titleLower` | `update_session_title` (same `update_item`, `SET title = :t, titleLower = :tl`) and the rename route | ≤ 50 B | `contains()` filter is case-sensitive; DynamoDB cannot lowercase. |
| `firstPrompt` | The title generator already holds the first user message (2,000 chars) when it writes the title; write the first 300 characters, lowercased, in the same call. | ≤ 300 B | Lets "BIO 101 syllabus" match a conversation titled "Course document review". Zero extra calls. |

A one-shot backfill, `backend/scripts/backfill_session_search_attrs.py`, copies `title → titleLower` for existing rows. Copy the shape of `backfill_session_static_sk.py`: dry-run default, `--apply`, `--sleep`, idempotent `attribute_not_exists(titleLower)` condition. `firstPrompt` is not backfilled (the text is in Memory events, and only for 90 days); older sessions match on title and summary only.

Session delete already removes the row from `SessionRecencyIndex` and purges the summary namespace, so neither path needs a new hook. The `PUT /metadata status=deleted` inconsistency (no GSI4 removal, no purge) predates this spec; search would list such sessions until it is fixed, and it should be fixed in its own PR.

## 5. API

One new route on app-api, cookie auth (`Depends(get_current_user_from_session)`), 404 while `CONVERSATION_SEARCH_ENABLED` is off.

```
GET /sessions/search?q=<text>&limit=20&mode=all|lexical
→ { "results": [ { "sessionId", "title", "lastMessageAt", "projectId"?, "assistantId"?,
                  "matchKind": "title" | "prompt" | "summary",
                  "snippet": "<≤200 chars>", "score": <float|null> } ],
    "semanticAvailable": true|false }
```

**Lexical leg** (`apis/shared/sessions/metadata.py`, `search_user_sessions_lexical`): Query `SessionRecencyIndex` with `GSI4_PK = USER#{uid}`, `FilterExpression contains(titleLower, :q) OR contains(firstPrompt, :q)`, `ProjectionExpression` limited to the fields above. The filter runs server-side over the user's partition; the read cost is the user's rows (a heavy user with 500 sessions and a few 32 KB `compaction.summary` maps is still a few MB, i.e. well under a cent). Cap the scan at 2,000 rows and stop. Normalize `q` the same way `titleLower` was written. Match kind is `title` if the title matched, else `prompt`. Snippet is the title or the matching window of `firstPrompt`.

**Semantic leg** (`apis/app_api/memory/services/memory_service.py`, `search_session_summaries(user_id, query, top_k)`): `retrieve_memories(namespace=f"/strategies/{summary_id}/actors/{user_id}/", query=q, top_k=10)`. Parse the session id from the record's namespace. Flatten the XML to the first `<topic>` body, trim to 200 characters. Then `BatchGetItem` the session rows by `(USER#{uid}, S#{sid})` to attach title and `lastMessageAt`, and drop any whose row is missing or `deleted` (a record whose session was deleted through the inconsistent metadata path).

**Merge:** lexical title hits first (exact recall of what the user typed beats inference), then semantic by score, then prompt hits; dedupe by session id, keep the first match kind. `limit` default 20.

**Mode `lexical`** is what the SPA sends on each keystroke; `all` is sent on Enter or after a 600 ms pause with ≥ 3 characters. This keeps `RetrieveMemoryRecords` to roughly one call per search, not one per keystroke, which matters for the 30/s account quota more than for the bill: the per-turn fact retrieval on the chat path shares it. A per-user token bucket (10 semantic searches per minute) in the route is the backstop; over the bucket the route serves lexical only and sets `semanticAvailable: false`.

**Project scope.** When the SPA is inside a project view, pass `projectId`; the lexical leg switches to `ProjectSessionIndex` and the semantic leg filters hits by the row's `preferences.projectId`.

**Not persisted:** nothing. Searches write no rows. The query text goes to AgentCore Memory as a search string, which is the same service the user's prompts already go to.

## 6. Frontend

- **Search box** in `components/sidenav/components/session-list/` above the grouped list; `Cmd/Ctrl+K` focuses it (first keyboard shortcut in the app; keep it to this one). Query kept in a signal; survives refresh via `sessionStorage` (try/catch).
- **Keystroke path:** filter the already-loaded `sessions()` by `titleLower` client-side at once (PR-1 behaviour), *and* fire `mode=lexical` through a `debounceTime(250)` + `switchMap` pipeline (copy `projects/components/people-picker.component.ts:145-176`). Server lexical results supersede the client filter when they arrive, so unloaded pages appear.
- **Enter / pause:** `mode=all`. Semantic rows render below with a muted "from summary" label and the snippet. A spinner only on this leg.
- **Result row** = the existing session row template (title, streaming/unread affordances, options menu) plus a snippet line and the match-kind glyph, so result rows and list rows stay one component.
- **Empty and error states:** "No matching conversations" vs the existing "No conversations yet"; if `semanticAvailable` is false, show lexical results with a one-line "Search by meaning is busy, showing title matches" and no retry loop.
- **Deep link to a message** is out of scope: summaries are per session, not per message, and assistant messages have no DOM anchor today. Revisit with PR-5 if it ships, since turn-level vectors would carry a message index.
- **#1380 also asks for artifacts and agents.** Agents can filter client-side over the already-loaded catalog in the same box (a second section). Artifacts need a list endpoint check before committing. Both are additive sections in the same results panel and belong in their own PR after PR-3.
- Flag: `features.conversationSearch` gates the server legs and the shortcut; PR-1's client filter is unconditional.

## 7. Phasing and PR plan

| PR | Scope | Flag | Size |
|---|---|---|---|
| **PR-1** SPA title filter | Search box, client-side filter over loaded sessions, Escape, live count, "No matching" state, `inert` on the closed off-canvas panel (the kaizen queue item verbatim) | none | ~1 day |
| **PR-0** probe | `audit.py search` + readout recorded in §3 | none | ½ day |
| **PR-2** backend | `titleLower`/`firstPrompt` writes, backfill script, `GET /sessions/search` with both legs, token bucket, `CONVERSATION_SEARCH_ENABLED` reader + CDK + `platform.yml` var, moto + stubbed-memory tests | backend | ~2 days |
| **PR-3** SPA server search | Debounced lexical, Enter semantic, merged results panel, project scoping, `features.conversationSearch`, `Cmd+K` | frontend | ~2 days |
| **PR-4** (optional) agent tool `search_my_conversations` | Catalog entry, `enabledByDefault: false`; same service; results bounded to 5 × 200 chars. Spec is ~150 tokens in `toolConfig`, so it is a feature switch, never default-on silently | RBAC + flag | ~1 day |
| **PR-5** (conditional) S3 Vectors turn index | Only if PR-0 fails its gate or verbatim in-message search is requested. Separate index `conversation-search-v1` on the existing vector bucket, `user_id` filterable, Titan v2 per turn from an after-invocation hook (never before first token), delete hook, 90-day backfill | backend | ~4 days |

Promotion: when PR-3 has been on dev for two weeks, flip the three flags to default-on with `=false` kill switches. This is a **feature switch**, not a rollout switch: a deployment without AgentCore Memory, or one that does not want search traffic against its memory quota, legitimately turns it off.

## 8. Cost summary

Per 1,000 searches: ≈ $0.50 (semantic leg) + < $0.01 (lexical leg). Per month at today's usage, assuming one in five monthly users searches three times: ≈ $2. At the 30,000-user planning target with the same ratio: ≈ $35. Storage added: ~350 bytes per session row. Prompt tokens added: zero (PR-4 excepted, ~150 tokens in the static prefix for users granted the tool).

For comparison, the summaries this leans on already cost about $33/month in prod (44,627 records × $0.75/1k). Search makes that spend do a second job.

## 9. Testing focus

- Lexical leg: normalization round-trips (`titleLower` written == query normalized), legacy rows without `titleLower` are skipped not crashed, project scoping uses GSI5, 2,000-row cap stops.
- Semantic leg: namespace prefix is the actor's (never another actor's), session id parsed from `namespaces[0]`, deleted/missing rows dropped, XML flattening on real record shapes from dev (fixtures from PR-0, identifiers scrubbed), throttle (`ThrottledException`) degrades to lexical with `semanticAvailable: false`.
- Merge: dedupe keeps the first kind; lexical title before semantic; `limit` honoured.
- Route: 404 when flag off; cookie auth; token bucket.
- SPA: filter and server results do not double-list a session; Escape restores the grouped list; refresh keeps the query; `inert` on the hidden panel.
- Smoke: a two-turn conversation on dev is findable by title within seconds and by paraphrase within ~2 minutes.

## 10. Open questions

1. **Archived sessions.** `status=archived` rows are off `SessionRecencyIndex`. Include them via a second lexical query, or leave archived out of search? Proposed: out of v1.
2. **Forked sessions** (share forks write events with `extractionMode="SKIP"`) will never have summaries. Acceptable, or should the fork copy the source session's summary records under the new session namespace? Proposed: acceptable; they still match by title and opening prompt.
3. **Quota headroom.** 30/s on `RetrieveMemoryRecords` is account-wide. Per-turn fact retrieval already uses it. Confirm on dev that a burst of 10 searches alongside live turns produces no `ThrottledException` before promoting; the token bucket is the backstop, not the plan.
4. **Local mode** (no AgentCore Memory) gets lexical only. Fine for dev laptops; worth a note in the docs-site flag row.
5. **Retention.** This spec deliberately does not copy message text anywhere. If the product later wants in-message phrase search older than 90 days, that is a retention change (where transcripts live and for how long) and gets its own spec before PR-5.
