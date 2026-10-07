# Conversation search

**Status:** DRAFT plan, written 2026-10-06 against `develop` @ `6f40bbe1`, when nothing was built. Revised the same day after the author chose to accept new resources where they serve users better (§2 records the no-new-resources alternative), and decided: archived sessions and shared forks are indexed, and one retention variable governs Memory events, the archive and the index (§3). **Amended 2026-10-06 (after PR-1 #1449 and PR-R #1450 merged):** the search surface is a `Cmd/Ctrl+K` modal that searches conversations, projects, agents and artifacts in one place (§1, §6, §6a), not a results list inside the sidebar; PR-4 is re-scoped and PR-4b added (§7). The backend design (§3–§5) is unchanged by this.
**Configuration introduced:** `CONVERSATION_RETENTION_DAYS` / `CDK_CONVERSATION_RETENTION_DAYS` (default 365), the single retention setting, and `CONVERSATION_RETENTION_PRUNES_SESSIONS` (default on, `=false` opts out), which lets that setting also remove session rows (§3).
**Tracking issue:** #1380 ("Enh: Search" — a user asks for ChatGPT-style search over conversations, artifacts and agents)
**Flags (in development, default OFF):** `CONVERSATION_INDEX_ENABLED` / `CDK_CONVERSATION_INDEX_ENABLED` (write path) and `CONVERSATION_SEARCH_ENABLED` / `CDK_CONVERSATION_SEARCH_ENABLED` / `features.conversationSearch` (read path). Two flags because indexing has to run ahead of search (§7). The SPA title filter (PR-1) ships with no flag: it costs nothing and touches no API.
**Related:** `docs/specs/bedrock-managed-kb-evaluation.md` (Managed KB shapes, measured latency, pricing), `docs/specs/memory-baseline-decision.md` (summary records census), `docs/specs/session-metadata-static-sort-key.md` (row shape, GSIs), `docs/kaizen/review-queue.md` §"sidebar conversation search".

## 0. Read this first: what the repo already has, and one thing it is losing

1. **Message text lives only in AgentCore Memory short-term events, and they expire after 90 days.** There is no DynamoDB or S3 transcript copy (`apis/shared/sessions/messages.py:359-569` reads `list_events`; `infrastructure/lib/constructs/agentcore/memory-construct.ts:74` sets `eventExpiryDuration: 90`). The session row has no TTL. **Measured on prod 2026-10-06 (read-only scan of `sessions-metadata`): 13,057 active sessions, of which 1,439 (11%) last moved before 2026-07-08.** Those conversations are still in every user's sidebar and open as "No messages yet". Dev: 89 of 1,225. This is a retention bug before it is a search problem, and any search that surfaces old conversations makes it visible. §3 fixes it first.
2. **The Managed Knowledge Base path is live in dev and prod** (`MANAGED_KB_MIGRATION_ENABLED=true` on the deployed task definition; `apis/shared/kb_backend/managed_backend.py`, `provisioning.py`). It already does the things a conversation index needs: inline-text ingestion with up to 50 metadata attributes per document, re-ingesting a document id replaces it (`managed_backend.py:577`), retrieval is hybrid (keyword + semantic) with a managed reranker (`managedSearchConfiguration`, `rerankingModelType: "MANAGED"`), and isolation filters are restricted to `equals` / `in` by `validate_isolation_filter` (`managed_backend.py:194-215`). Measured: `CreateKnowledgeBase` → ACTIVE in 84–97 s, first ingest ~65 s warm-up, later ingests 4–6 s, retrieve p50 672 ms (`bedrock-managed-kb-evaluation.md`).
3. **Managed KB pricing has no floor** (verified against the Price List API, service code `AmazonBedrockAgentCore`): $5.00 / GB-month stored, $0.001 per Retrieve, nothing per KB and nothing per hour. Quotas (Bedrock quotas page, 2026-10-06): 10,000 KBs per account, 100 Retrieve requests/s per account (adjustable), 600 Retrieve/min per KB (adjustable), 20 `IngestKnowledgeBaseDocuments`/s (adjustable), 10 documents per ingest call, 10 TB per KB.
4. **Every session also has an AgentCore summary record** (summary strategy, prod holds ~45k records, median 1,409 chars) that is purged on session delete and searchable across all of a user's sessions in one `RetrieveMemoryRecords` call, because that API matches `namespace` as a prefix. This is the zero-new-resources route (§2, option C). It finds conversations by topic but not by phrase, cannot point at a message, and has no text behind it for the 11% above. It stays documented as the fallback for a deployment that turns the index off.
5. **Nothing searches sessions today.** No search box, no `Cmd+K`, no `q` parameter on `GET /sessions` (`sessions/routes.py:64-119`). The sidebar pages 30 at a time over `SessionRecencyIndex`. User messages carry a `message-{id}` DOM anchor and `MessageListComponent.scrollToMessage` exists (`message-list.component.ts:923-930`); assistant messages have no anchor.

## 1. What the user gets

Two surfaces, one search.

**The sidebar filter (PR-1, shipped).** A box at the top of the conversation sidebar narrows the loaded list by title as the user types. It costs nothing, needs no flag and stays exactly as it is. It is the right tool when the conversation was open this week and the user remembers roughly what it was called.

**The search modal (this amendment).** `Cmd/Ctrl+K` anywhere in the app, or the search button beside "New Session", opens a centered dialog with one input. It is the right tool for everything else: a phrase from months ago, a project, an agent, an artifact the user made in some conversation they cannot name. Before anything is typed it shows the user's most recent conversations and a few actions (new conversation, new project, new agent), so it is also the fastest way back to yesterday's work. Typing filters every scope at once; a row of chips (All, Conversations, Projects, Agents, Artifacts) narrows to one. Conversations are searched by title instantly and by full text on Enter or a pause, with a highlighted snippet of the matching passage. Choosing a conversation opens it **scrolled to the matching turn**. Escape closes.

The two are connected: with the modal available, Enter in the sidebar box hands its query to the modal, so a quick filter that found nothing becomes a full search in one keystroke.

What "search the text" means here, because it is what #1380's screenshot shows: a remembered phrase ("window function"), an identifier ("BIO 101", a file name, an error code), or a topic in the user's own words ("the syllabus rewrite") all find the conversation, and the snippet shows *where*. Hybrid retrieval gives the first two by keyword and the third by meaning, in one call, reranked.

Why a modal rather than results in the sidebar (the original plan): #1380 asks for conversations, artifacts and agents together, and the sidebar is a list of conversations. Putting projects and artifacts into it would either misfile them or need a second sidebar. A dialog has room for sections, is reachable from every page (the sidebar is not: `/admin`, the artifact viewers and the minimal-chrome routes hide it), and matches what users already know from ChatGPT and Claude, both of which moved to this shape.

Nothing in this plan touches the model call path, the system prompt, `toolConfig` or restored history. The cacheable prefix is unchanged. Time to first token is unchanged: the only write on the turn path is a fire-and-forget S3 put after `done` (§4). The one optional item that reaches a prompt (an agent-facing tool, §7 PR-5) is called out separately and defaults off.

## 2. Options considered

Scale basis: 13,057 active prod sessions today, ~2,000 new sessions a month (1,350 costed sessions over three weeks in September), ~5 model calls a session, ~15 KB of text a session, 1,807 monthly active users, 30,000-user planning target. Searches assumed at one in five monthly users, three searches each.

| Option | Finds | Monthly cost today | At 30k users | Verdict |
|---|---|---|---|---|
| **A. Client-side title filter** over loaded sidebar pages | titles, loaded pages only | $0 | $0 | Ship first (PR-1). The kaizen queue item. |
| **B. Server-side lexical** (`titleLower` + `firstPrompt` attributes, DynamoDB query over the user's partition) | titles, opening prompt, all pages | < $0.05 | < $1 | Ship (PR-4). Instant keystroke feedback; no new infra. |
| **C. Semantic over existing AgentCore summaries** (prefix `RetrieveMemoryRecords`, $0.50/1k) | topic, per session, no snippet from text | ~$0.60 | ~$10 | **Fallback only.** Zero new resources, but no phrase match, no jump-to-message, and nothing behind the 11% whose events expired. Documented, not built unless a deployment cannot run the index. |
| **D. Own S3 Vectors turn index** (Titan v2, per-turn hook, `user_id` filterable metadata) | meaning, per turn | ~$1 (embeddings $0.02/MTok, storage $0.06/GB-mo, PUT $0.20/GB with 128 KB minimum, queries $2.50/M) | ~$15 | Reject in favour of H. Same engineering surface as H minus keyword search, reranking and managed chunking. |
| **E. Per-user Managed KB** | as H | $0 floor, but 10,000 KBs per account | fits the quota, but 65 s first-ingest warm-up per user and a per-user retrieve quota | Reject. One shared KB with a `user_id` filter is what the repo already does for assistants. |
| **F. OpenSearch Serverless** (BM25 + kNN + highlights) | everything | ~$175–700 fixed floor | same | Reject. The floor is 50× H's bill and H already delivers hybrid search and highlights. |
| **G. Own Nova Micro "search card" per session** | topic | ~$0.10 | ~$2 | Reject: duplicates C with a model we pay for. |
| **H. One shared Managed KB over per-turn transcript documents**, fed from an S3 transcript archive | phrase + meaning, per turn, snippet, jump-to-message, all history | **~$4** (storage 200 MB → $1; retrieves ~1,100/mo → $1; S3 puts and events → cents; ingestion → no SKU) | **~$45** (storage ~3 GB → $15; retrieves ~18k → $18; rest → cents) | **Ship (PR-2 to PR-4).** |

Pricing sources checked 2026-10-06: AgentCore Memory (aws.amazon.com/bedrock/agentcore/pricing: short-term storage $0.10/GB-month, long-term retrieval $0.50/1k); Managed KB (Price List API, `AmazonBedrockAgentCore`: `Knowledge-Base:Consumption-based:Storage` $5/GB-month, `:Retrieval` $0.001/query); S3 Vectors (aws.amazon.com/s3/pricing); Titan Text Embeddings V2 $0.02/MTok; OpenSearch Serverless from its OCU floor.

**Why H.** It is the only option that answers the request as asked: find by phrase or meaning, show the passage, jump to it, across all history. Its marginal cost over C is a few dollars a month at today's scale. Its engineering cost is mostly code the repo already has (ingest, isolation filter, provisioning, the EventBridge-to-consumer pattern in `kb-migration-construct.ts:637-668`). And the S3 archive it is fed from is the same durable transcript copy that closes the retention gap for good, so one write serves two needs.

## 3. Retention: one variable, every copy

Today the only retention setting is the literal `90` in `memory-construct.ts:74`, and it governs one of the places a conversation will live. Once there are three (Memory events, the S3 archive, the KB documents), three independent clocks would drift the first time one is tuned. So retention becomes **one deployment variable** that every copy follows:

```
CDK_CONVERSATION_RETENTION_DAYS   (GitHub environment variable → platform.yml → config.ts via parseIntEnv)
  default 365; unset or "" → 365
```

| Copy | How the variable is applied | Mechanism |
|---|---|---|
| **Memory events** | `eventExpiryDuration: min(n, 365)` on `CfnMemory` | CloudFormation *Update requires: No interruption* for this property on `AWS::BedrockAgentCore::Memory`; API range 3–365. Memory cannot hold more than a year, which is why the archive exists. |
| **S3 archive objects** | Bucket lifecycle rule, `expiration: Duration.days(n)` on the `conversations/` prefix | Per object, from its write time, so a long-running conversation loses its oldest turns first, exactly as Memory does. |
| **KB documents** | Deleted when their archive object expires | S3 lifecycle expiry emits an EventBridge *Object Deleted* event with `reason: "Lifecycle Expiration"` (and `deletion-type: "Permanently Deleted"`, because the bucket is unversioned). The same consumer Lambda that ingests on *Object Created* calls `DeleteKnowledgeBaseDocuments` on *Object Deleted*, whatever the reason, so an expiry and a user delete take the same path. |
| **Read path, as a belt** | A result whose `created_at` attribute is older than `n` days is dropped before it is returned | Covers the window between S3 expiry (which S3 runs asynchronously, up to about a day late) and the delete event landing. Done in code after retrieval, not as a KB filter, so `validate_isolation_filter` stays `equals`/`in` only. |
| **Daily reconciler, as braces** | `rate(1 day)` Lambda (the `kb-migration` reconciler pattern) that lists archive objects past `n` days and KB documents without an object, and deletes both | Catches a dropped event or a DLQ'd batch. Read-only in dry-run; writes behind the same `CONVERSATION_INDEX_ENABLED` switch. |

**The session row** follows the variable too, which is what closes the cliff: today the row outlives every message behind it. The daily reconciler runs the existing `SessionService.delete_session` on sessions whose `lastMessageAt` is older than `n` days, so a conversation past retention leaves the sidebar instead of opening empty. That path already purges summary records, files and shares, and leaves the soft-delete tombstone, so nothing new is written for it. **Pruning is on by default**, with `CONVERSATION_RETENTION_PRUNES_SESSIONS=false` as the opt-out for a deployment that prefers title-only shells to deletions (a feature switch, permanent; the docs-site flag page records it). Cost rows (`C#`) keep their own 365-day TTL and are not touched. Decided 2026-10-06.

Two guards on the first run, because pruning deletes rows users can see:
- **The first prod run is a dry run** that reports the count and the oldest and newest `lastMessageAt` it would delete; the 1,439 sessions found on 2026-10-06 are the expected order of magnitude. The second run deletes, throttled (`--sleep`, as in the backfill scripts).
- **It never deletes a session that is `lastTurnContinuable` or holds a pending interrupt**, and it skips a row whose `lastMessageAt` is missing rather than treating it as old.

What the variable means to a deployment, in one sentence for the docs: *a conversation's content is kept for this many days after each turn, wherever it is stored; nothing about a user's long-term memory records (facts, preferences) changes.*

**Why 365 and not more.** Memory caps at 365, and the archive is what the messages route reads when Memory is empty, so a larger value would work for the archive and the index. But a conversation older than a year has no Memory events either, and the first setting should be one every copy can honour. Raising it later is a one-variable change plus a lifecycle-rule update; objects already past the old rule are gone.

**Verify on dev before prod:** whether raising `eventExpiryDuration` applies to events already written. The API documentation does not say. If existing events keep their 90-day clock, the backfill (§7 PR-3) is the rescue for sessions between 90 and 365 days old on the day the change lands: it copies every event still alive into the archive, and the archive is what the messages route reads from then on.

The 1,439 prod conversations already past the cliff cannot be recovered; their events are gone. Their rows stay (until pruning is turned on), and they match by title only.

## 4. Data flow

```
turn ends (`done` emitted)
  └─ fire-and-forget, off the TTFT path, in stream_coordinator next to the title persist:
       put_object  s3://{prefix}-conversation-archive/conversations/{user_id}/{session_id}/{message_index:06d}.json
         body: {schemaVersion, sessionId, userId, messageIndex, userText, assistantText,
                createdAt, projectId?, assistantId?}        (tool text dropped; no title, see below)
         ↓ S3 event → EventBridge rule (prefix match) → SQS (DLQ) → conversation-index consumer Lambda
IngestKnowledgeBaseDocuments (batches of 10, ≤ 20 rps)
   knowledgeBaseId = the one shared `conversations` KB for the environment
   customDocumentIdentifier = "conv#{session_id}#{message_index}"
   inlineContent = "{user text}\n\n{assistant text}"       (tool results excluded; see below)
   inlineAttributes = user_id, session_id, message_index, created_at, project_id?, assistant_id?
```

**Document = one turn** (the user message and the assistant reply that answered it), not one session. A turn is the unit the user remembers and the unit a result can jump to: the hit's `customDocumentIdentifier` gives `session_id` and `message_index`, and the SPA scrolls to the user message's existing `message-{id}` anchor. Re-ingesting a document id replaces it, so a retried archive write is idempotent. Session title is *not* in the document text (it would match every turn of a session); it is attached at read time from the session row.

**Tool results are not indexed.** They are the bulk of a session's bytes, they are the part most likely to contain third-party data pulled from a tool, and nobody searches for them by memory. User text and assistant text only, capped at 16 KB per document (a long assistant answer still fits; Managed KB chunks internally).

**How the write path places a turn (as built in PR-2a).** The coordinator already knows the global index of the turn's last assistant message (`message_id`, the same number its cost rows and `displayText` use). The turn's opening user message is found by walking `agent.messages` back to the last user message that carries text and no `toolResult` (a steering follow-up rides on a tool-result message, so it stays inside the running turn), and its index is `message_id` minus the distance. Compaction trims the head of that list, never the tail, so this holds on long sessions. The user's words come from `original_message` when the prompt was modified; when the turn began in an earlier request (a resume after an interrupt, a continuation), the stored `displayText` row is read in the background instead, so retrieved KB excerpts and attachment guidance never reach the index. A paused turn is archived when it pauses and re-archived under the same key when it resumes. The runtime resolves the bucket from SSM `/{prefix}/conversations/archive-bucket-name`, once per process, because its 50 environment variables are nearly spent (47 used); it spends one slot, on the flag. The cut-a-turn code (`apis/shared/conversation_archive/documents.py`) is pure, so the fork path and PR-3's backfill produce byte-identical objects from the same history.

**The archive object is the durable transcript** (§3). It is written once per turn, before ingestion, so the index can always be rebuilt from it, and the messages route can read it when Memory has nothing. The bucket is private, SSE-S3, **unversioned**, with the retention lifecycle rule from §3; otherwise the same posture as `shared-conversations`.

**Shared forks are indexed under the forker.** A fork copies the snapshot's messages into the new session's Memory directly (`shares/service.py`, `_copy_messages_to_memory`), never through a turn, so the archive put must also run there: one object per copied turn, under the forker's user id and the new session id. Those events carry `extractionMode="SKIP"` so they never become the forker's long-term records; that stays true, since the index reads the archive, not Memory's extraction. The forker can already read every one of those messages, and the conversation is theirs from the fork on.

**Archived sessions are indexed and searchable.** Archiving changes a row's `status`; it does not touch Memory, the archive or the KB, and nothing in the write path consults status. The read path tags the result (§5, §6).

**Delete.** `SessionService.delete_session` gains one background step beside the existing memory purge: list the session's archive prefix and delete the objects. Each deletion raises *Object Deleted* on EventBridge and the consumer removes the matching KB document (`DeleteKnowledgeBaseDocuments`, batches of 10, ≤ 10 rps), which is the same path a retention expiry takes (§3). Project deletion and user deletion go through the same per-session path. A document whose session row is gone or `deleted` is also dropped at read time, so a lagging delete never shows a result.

**Provisioning.** One Managed KB per environment, created lazily on first index write through the existing `provision_managed_kb` with the reserved app KB id `conversations` (84–97 s once, then stored in SSM `/{prefix}/conversations/kb-id`). Not CDK, for the same reason the assistant KBs are not. Tag it `purpose=conversation-search` so the cost line is attributable (`managed-kb-cost-attribution.md`).

**Quotas at scale.** One KB serves all users behind a `user_id equals` filter, exactly as the legacy index serves all assistants behind `assistant_id`. 600 Retrieve/min per KB covers 30k users at the assumed rate with a wide margin (~18k searches a *month*); it is adjustable, and sharding by user-id hash is a one-line change in the provisioning key if ever needed. Ingest at 2,000 sessions × 5 turns a month is ~0.004/s against a 20/s quota.

## 5. API

One new route on app-api, cookie auth (`Depends(get_current_user_from_session)`), 404 while `CONVERSATION_SEARCH_ENABLED` is off.

```
GET /sessions/search?q=<text>&limit=20&mode=all|lexical[&projectId=]
→ { "results": [ { "sessionId", "title", "lastMessageAt", "projectId"?, "assistantId"?,
                  "messageId"?: "msg-{sessionId}-{index}",
                  "matchKind": "title" | "prompt" | "text",
                  "snippet": "<≤240 chars, match window>", "score": <float|null> } ],
    "textSearchAvailable": true|false }
```

**Lexical leg** (`apis/shared/sessions/metadata.py`, `search_user_sessions_lexical`): Query the base table, `PK = USER#{uid} AND begins_with(SK, "S#")`, `FilterExpression (status = active OR status = archived) AND (contains(titleLower, :q) OR contains(firstPrompt, :q))`, projection limited to the fields above, scan capped at 2,000 rows. The base table rather than `SessionRecencyIndex` because archived rows are off that index, and a user's `S#` rows are the same partition either way. `titleLower` and `firstPrompt` (first 300 lowercased characters of the opening prompt) are written in the same `update_item` that writes the title (`update_session_title`, `metadata.py:1320`), plus the rename route; a dry-run-default backfill script copies `title → titleLower` for existing rows (copy `backfill_session_static_sk.py`).

**Text leg** (`apis/shared/kb_backend/managed_backend.py`, reuse `retrieve` with `retrieval_filter={"equals": {"key": "user_id", "value": uid}}`, and `andAll` with `project_id` when scoped): `numberOfResults=20`, managed reranking on. Parse `session_id` and `message_index` from the custom document identifier. `BatchGetItem` the session rows `(USER#{uid}, S#{sid})` for title, `lastMessageAt`, status; drop missing or `deleted`, keep `archived` and mark it (`"archived": true` on the result). Drop any hit whose `created_at` is past retention (§3). Snippet is the chunk text windowed around the first query-term hit (or its first 240 chars when the match was semantic). Collapse to one result per session, keeping the best-scoring turn; the next two turns' indexes ride along as `alsoMatched` so the SPA can offer "3 matches in this conversation".

**Merge:** exact title hits first, then text hits by reranked score, then prompt hits; dedupe by session id. `limit` default 20.

**Modes.** `lexical` on every keystroke (DynamoDB only); `all` on Enter or after a 600 ms pause with ≥ 3 characters. Retrieve is ~670 ms p50, so it is never fired per keystroke. A per-user token bucket (20 text searches a minute) is the backstop against the per-KB quota; over it, the route returns lexical results with `textSearchAvailable: false`.

**Nothing persisted** by a search. The query text goes to Bedrock as a retrieval query, the same service the user's prompts already go to.

## 6. Frontend

Two components share one search service. The sidebar filter is done (PR-1); the rest of this section is the modal.

- **Shell.** `components/search/search-dialog.component.ts`, opened through `@angular/cdk/dialog` exactly like `projects/components/create-project-dialog.component.ts` (same backdrop, same `DialogDismissDirective`, same panel classes), so it inherits focus trapping and the app's dialog look without a new pattern. One root-provided `SearchDialogService` owns `open(query?)`: a second `open` while it is up focuses the input instead of stacking. Below `sm` the panel is a full-height sheet.
- **Opening it.** `Cmd/Ctrl+K` is bound once, on the app shell (`document:keydown` in `App`), the app's first global shortcut; it is ignored while the modal is open, while a CDK dialog is already open, and in editable targets other than the sidebar filter box. A search button (magnifier) sits in the sidenav header beside "New Session" for discoverability and for touch. Enter in the sidebar filter box opens the modal pre-filled with its query. All three are gated by `features.conversationSearch`; with the flag off none of them exist and the sidebar filter behaves as PR-1 shipped it.
- **Query path (Conversations scope).** Keystroke: filter the loaded `sessions()` by `titleLower` client-side at once, and fire `mode=lexical` through `debounceTime(250)` + `switchMap` (copy `projects/components/people-picker.component.ts:145-176`); server results supersede the client filter so unloaded pages appear. Enter, or a 600 ms pause with ≥ 3 characters: `mode=all`. Text hits render with the snippet and the match terms emphasised; a spinner only on this leg. Query kept across refresh in `sessionStorage` (try/catch), separate from the sidebar filter's key.
- **Result rows.** Conversation = title, when it last moved, snippet line, small "Archived" tag when `archived: true` (opens read-only exactly as from the manage-sessions page). Project = name and description. Agent = name, with a "Public" tag for one the user does not own. Artifact = title, content type, when it last changed.
- **Jump to message:** navigate to `/s/:id?m=msg-…`; `ConversationPage` reads `m` from `queryParamMap` (it already reads `assistantId` there) and, once messages load, calls `scrollToMessage` instead of `restoreScrollPosition`. Only user messages have anchors today; a hit on a turn scrolls to that turn's user message, which is where the assistant's answer begins. Good enough; assistant anchors are not needed for v1.
- **Empty and degraded states.** No query: Recents and Actions (§6a). A query with nothing in any scope: one "No matches" line, never five empty sections. `textSearchAvailable: false`: lexical rows plus one muted line ("Showing title matches only"), no retry loop. A scope whose fetch failed shows its own one-line error and leaves the other scopes alone.
- **Files are not a scope in v1.** `GET /files` pages 20–100 ready files and has no name parameter, so a client filter would only see the first page, which the artifact library's own comment (`artifact-library.page.ts:170-175`) rightly calls worse than no filter. Adding a server `q` to the files route is a small, separate change; it is listed under "later" in §7.

### 6a. The modal, in detail

**Layout, top to bottom.** Input row (search icon, `<input>`, Clear when non-empty, Close). Scope chips: All · Conversations · Projects · Agents · Artifacts, as a `role="tablist"`; All is selected on open. Body: in All, one section per scope that has matches, in that fixed order, capped at five rows each with a "Show all N" footer row that selects that scope; in a single scope, the full list. Footer: key hints (Esc close · ↑↓ move · ↵ open · ←→ change scope), the same strip Claude's palette shows, because the hints are what make the keyboard path discoverable.

**Why fixed section order and no cross-scope ranking.** A conversation's text score, a project's name match and an artifact's title match are not on one scale; interleaving them would put a weak conversation hit under a strong artifact title and look random. Conversations lead because they are what #1380 asks for first and what the text leg is built for.

**Before a query.** Two sections. *Recent:* the eight most recent conversations from the sidebar pages already in memory (no request; it is the same `sessions()` signal the sidebar renders). *Actions:* New conversation (`/`), New project (opens the existing create-project dialog; only when `features.projects`), New agent (`/agents/new`). Nothing else: the empty state should load in zero round trips and never be blank.

**Where each scope's data comes from.** Conversations: `GET /sessions/search` (§5). Projects: the `ProjectsService` list the nav already loads, filtered client-side on name and description. Agents: the assistants the user owns plus public ones, from one `GET /assistants?include_public=true&limit=1000` fetched the first time the modal opens in a page load and held until the next full load; filtered client-side on name and description. Artifacts: `GET /artifacts/library` fetched lazily the first time the Artifacts scope has a query, same lifetime; filtered client-side on title (the library page already does exactly this, over the same one-page response). Each of these is one DynamoDB query the user already triggers by visiting the corresponding page, so the modal adds nothing to §8's cost table.

**Keyboard model.** Focus stays in the input the whole time (combobox pattern: `role="combobox"`, `aria-expanded`, `aria-controls`, `aria-activedescendant` pointing at the highlighted row). ↑/↓ move the highlight across section boundaries; ↵ activates it; ←/→ change scope when the caret is at the start or end of the input, otherwise they move the caret; Tab moves to the chips and footer as normal, so a screen-reader user who does not know the shortcuts still reaches everything. A `aria-live="polite"` region announces the result count after each search settles ("12 conversations, 2 projects").

**Motion.** Panel fades and lifts over 150 ms using the dialog's existing enter/exit classes; the Recent → results swap is a crossfade, not a reflow jump; all of it is off under `prefers-reduced-motion`.

**Behaviours the sidebar filter and the modal must agree on.** An untitled conversation is matched and shown as "Untitled Session" in both (`UNTITLED_SESSION_TITLE`). A deleted session never appears in either. Both read the same `sessions()` signal, so a conversation renamed in one is renamed in the other without a refetch.

## 7. Phasing and PR plan

| PR | Scope | Flag | Size |
|---|---|---|---|
| **PR-1** SPA title filter | Search box, client-side filter over loaded sessions, Escape, live count, "No matching" state, `inert` on the closed off-canvas panel (the kaizen queue item verbatim) | none | ~1 day |
| **PR-R** retention | `CDK_CONVERSATION_RETENTION_DAYS` → `config.ts` (`parseIntEnv`, default 365) → `eventExpiryDuration: min(n, 365)`; `platform.yml` forwarding; config test; docs-site row; dev check of whether existing events pick it up; CHANGELOG note. Ships ahead of everything else | none | ½ day + a dev deploy |
| **PR-2a** archive write path | Archive bucket (CDK, unversioned, lifecycle `expiration = n` days on `conversations/`, name in SSM), after-`done` S3 put in `stream_coordinator` and in the fork copy path, session-delete hook (single and bulk; not flag-gated), `CONVERSATION_INDEX_ENABLED` reader + CDK + `platform.yml` var, moto tests | index | ~1½ days |
| **PR-2b** index consumer | Bucket EventBridge notifications; *Object Created* and *Object Deleted* rules on `conversations/` → consumer Lambda (container image, same deploy pattern as `kb-sync`; the kb-migration consumer is invoked by EventBridge directly with SQS only as its DLQ, and that proven shape is the default here unless batching needs a queue) that ingests or deletes; lazy provisioning of the `conversations` KB; stubbed-bedrock tests | index | ~2 days |
| **PR-2c** reconciler + pruning | Daily reconciler (first run dry; archive objects past `n`, KB documents without an object; session pruning on by default, `CONVERSATION_RETENTION_PRUNES_SESSIONS=false` opts out) | index | ~1½ days |
| **PR-3** backfill | `backend/scripts/backfill_conversation_archive.py`: for every active session with live events, `list_events` → archive objects via `split_turns` (the consumer indexes them). Dry-run default, `--sleep`, idempotent (skip existing keys), `--user` for a single actor first. Run on dev, then prod, before PR-4 is enabled. Messages-route fallback to the archive when Memory has nothing (moved here from PR-2: it only matters once the backfill has rescued sessions Memory has dropped) | none | ~1½ days + run time |
| **PR-4** search route + modal | `titleLower`/`firstPrompt` writes + backfill, `GET /sessions/search` both legs, token bucket, `CONVERSATION_SEARCH_ENABLED`; the search dialog shell on CDK Dialog with the Conversations scope only (no chips yet), Recents + Actions empty state, `Cmd/Ctrl+K`, the sidenav search button, Enter-in-sidebar hand-off, jump-to-message, `features.conversationSearch` | search | ~4 days |
| **PR-4b** more scopes | Scope chips; Projects, Agents and Artifacts scopes over the existing list endpoints (§6a); All view with capped sections and "Show all"; ←/→ scope keys; live-region count. Pure SPA, no new routes, same flag | search | ~2 days |
| **PR-5** (optional) agent tool `search_my_conversations` | Catalog entry, `enabledByDefault: false`; same service; results bounded to 5 × 240 chars. ~150 tokens in `toolConfig`, so a feature switch never default-on silently | RBAC + flag | ~1 day |
| later | Files scope (needs a `q` parameter on `GET /files`, since the list pages and has no name filter); assistant-message anchors if users ask for finer jumps | | |

Promotion: once PR-4 has run on dev for two weeks with the backfill complete, flip the flags default-on with `=false` kill switches. Both are **feature switches**, not rollout switches: a deployment legitimately opts out of a second copy of transcripts or of KB spend, and `CONVERSATION_INDEX_ENABLED=false` must also stop the archive write. A deployment with the index off and `CONVERSATION_SEARCH_ENABLED=true` gets lexical only.

## 8. Cost summary

| Line | Today (13k sessions, ~2k new/mo) | 30k users |
|---|---|---|
| Managed KB storage, $5/GB-mo (text only, ~15 KB/session; index overhead assumed 2×) | ~$1 | ~$15 |
| Managed KB retrieve, $0.001 each | ~$1 | ~$18 |
| S3 archive (puts $0.005/1k, storage $0.023/GB-mo), EventBridge, SQS, Lambda | cents | ~$2 |
| AgentCore short-term memory at 365 days, $0.10/GB-mo | ~$1 | ~$5 |
| DynamoDB lexical queries | cents | < $1 |
| **Total** | **~$4 / month** | **~$40–45 / month** |

Prompt tokens added: zero (PR-5 excepted). Turn-path latency added: zero before first token; one async S3 put after `done`. Session row growth: ~350 bytes.

For scale, the summary records this plan no longer leans on already cost ~$33/month in prod. The whole search feature costs less than they do.

## 9. Testing focus

- Archive write: fires only after `done`, never blocks the stream, skipped for preview sessions, idempotent key, tool text excluded, 16 KB cap, flag off ⇒ no put.
- Consumer: batches of 10, respects 20 rps, DLQ on failure, document id format, attributes set, replace-on-reingest.
- Delete and expiry: session delete removes the objects and the consumer removes the documents; a synthetic *Object Deleted* event with `reason: "Lifecycle Expiration"` takes the same path; a result whose row is `deleted` or whose `created_at` is past retention is dropped at read time; the reconciler's dry run reports and writes nothing.
- Retention variable: `config.ts` clamps Memory at 365 while the lifecycle rule takes the raw value; unset and `""` both give 365; the memory construct test asserts the clamp.
- Forks and archived: a fork writes one archive object per copied turn under the forker's id; an archived session's turns stay indexed and the result carries `archived: true`; the lexical leg returns archived rows and excludes `deleted`.
- Isolation: every retrieve carries `user_id equals`; `validate_isolation_filter` rejects anything else; a test with two users' documents in one stubbed KB proves no cross-read.
- Lexical leg: normalization round-trips, legacy rows without `titleLower` skipped, project scoping uses GSI5, 2,000-row cap.
- Merge: dedupe keeps the best kind; one row per session; `alsoMatched` populated.
- Route: 404 when flag off; cookie auth; token bucket degrades to lexical.
- SPA, sidebar filter: client filter and server results do not double-list; Escape restores the grouped list; refresh keeps the query; `inert` on the hidden panel; Enter opens the modal with the query when the flag is on and does nothing when it is off.
- SPA, modal: `Cmd/Ctrl+K` opens it once and refocuses on a second press; ignored inside editable targets and while another CDK dialog is open; flag off ⇒ no shortcut, no button, no component in the bundle (`@defer`/lazy import); Recents are the eight most recent loaded sessions and need no request; ↑/↓ cross section boundaries and ↵ opens the highlighted row; ←/→ change scope only at the caret ends; scope fetches are lazy and cached for the page load; a failing scope shows its own error and the others still render; All caps each section at five with a working "Show all"; `aria-activedescendant` tracks the highlight; `?m=` scrolls to the turn; axe passes on the open dialog in light and dark.
- Smoke (dev): a two-turn conversation is findable by title within seconds and by a phrase from the assistant's answer within ~10 s of the turn ending; the result opens scrolled to that turn.

## 10. Open questions

1. **Does raising `eventExpiryDuration` extend events already written?** Decides whether PR-3's backfill is also the rescue for sessions between 90 and 365 days old on the day PR-R lands. Test on dev with a known old session.
2. **Governance.** The index is one shared KB isolated by identity-bound filters, the same posture as the legacy assistant index and the same data class as sessions behind the same JWT + RBAC. Nothing new is exposed; stating it here so the question is not re-opened per PR.

Decided 2026-10-06: archived sessions and shared forks are indexed (§4); retention is one variable across Memory, archive, index and session rows, with session pruning on by default (§3).

Amended 2026-10-06, after PR-1 and PR-R merged and the author compared the ChatGPT and Claude search palettes: the search surface is a `Cmd/Ctrl+K` modal over conversations, projects, agents and artifacts (§1, §6, §6a); the sidebar filter stays as the in-place quick filter; PR-4 ships the modal with the Conversations scope and PR-4b adds the others (§7); files wait on a server-side name filter.
