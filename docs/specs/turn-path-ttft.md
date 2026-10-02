# The turn path: request to first token

**Status:** assessment and plan, written 2026-09-29 against `develop` @ `e4646277`
(PR #1378 merged) with PR #1377 (`feature/agent-build-latency`) open.
**Progress (2026-10-02):** P2 shipped (#1395), P1a (#1396) and P1b (#1397, #1398) shipped
and read on dev, P4a shipped (#1400) and read on dev; P3a parked on the P1a readout. F5
(the per-turn history count) measured on a long conversation, then taken off the critical
path (#1403) and read on dev — see §5 P4. A KB agent's cold first turn spent 21.5s loading
two MCP servers in series; they now load concurrently (#1406, read on dev) — see §5 P3.
**Supersedes nothing; it joins three specs that each cover one slice of this path:**
- `docs/specs/turn-latency-preamble.md` — the preamble (455ms → 22–37ms warm) and the
  decomposition of `agent_build`. Its PR-5 (split `agent_build.tools`) shipped as P1a below (#1396) and was read on dev 2026-10-01.
- `docs/specs/agent-state-feedback.md` — PR-3 deferred the agent build into the stream and
  narrates it (`preparing` / `prepared`).
- `docs/specs/agentcore-runtime-v2.md` — the Runtime V2 migration and the prewarm-on-intent
  idea (§5a).
**Assesses:** PR #1377, *perf: A/B the first-turn agent build (shared Memory clients,
off-loop build), default off.*

Labels: **Measured** = a number from dev or prod with a source. **Read** = read off the code
on this date. **Hypothesis** = inferred, not observed. The preamble spec records what
happens when those get confused (its "~53ms per GSI query" turned out to be boto3 client
construction on a throttled CPU), so the labels are load-bearing.

---

## 1. Why this document exists

A turn is the most complicated process in the app and nobody had one map of it. The handler
in `apis/inference_api/chat/routes.py` is ~2,200 lines in one function with two nested
generators closing over ~40 locals; the build it triggers spans `service.py`,
`base_agent.py`, `chat_agent.py`, `agent_factory.py`, `session_factory.py`,
`turn_based_session_manager.py`, `external_mcp_client.py` and two SDKs; the stream that
follows starts in `stream_coordinator.py` and hands off to Strands. Each spec above opened
one window on it. This document is the whole path, stage by stage, with what runs where,
what is network, what blocks the event loop, and which `turn_prelude` mark (if any) sees it.

Three things fall out of drawing it:

1. **The plan** (§5): where the time still is on a cold first turn, ordered so every change
   is measured before it is trusted, and leaning toward Runtime V2.
2. **The PR #1377 assessment** (§4): it aims at the right stage, half the fix lands lazily,
   and the arm that carries all the complexity buys something other than what it measures.
3. **A regression plan a cheaper model can execute** (§6): invariants that must never move,
   named tests per change, and the dev recipes that settle timing claims.

---

## 2. The map

### 2A. Before the handler (no server-side clock sees any of this)

| # | Stage | Where | What happens | IO | Cost (warm / cold) |
|---|---|---|---|---|---|
| A1 | Send | SPA | `fetch-event-source` `POST /api/chat/stream` with an `InvocationRequest` body (`session_id`, `message`, `model_id`, `enabled_tools`, `enabled_skills`, `file_upload_ids`, `rag_assistant_id`, …). The SPA mints the session id client-side. | — | — |
| A2 | Edge + BFF | CloudFront → ALB → app-api `chat_stream` (`apis/app_api/chat/proxy_routes.py`) | Cookie auth (`get_current_user_from_session`: BFF session row; user upsert throttled to 300s), CSRF. Reads `session_id` out of the body and sets the **runtime affinity header** `sha256(session_id)` so the turn lands on the conversation's own microVM. Forwards `Authorization: Bearer <id token>` and `OAuth2CallbackUrl`. `httpx` `stream=True`; headers are relayed as soon as inference-api flushes them. | 1 DynamoDB read | **Measured** ~478ms warm for A2+A3 together (agent-state-feedback PR-3) |
| A3 | Data plane | AgentCore Runtime | Routes to the microVM pinned by the affinity header. **No microVM → cold start**: V1 pulls the image and boots the container (**Measured** cold turn 6.7s vs warm 3.75s end to end on 2026-09-19; AWS's V1 curve is 5.4s at 200MB); V2 restores a snapshot (**Asserted** ~2s flat). `/ping` is polled ~every 2s. Only `POST /invocations` and `GET /ping` are proxied. | — | 0 / 1.5–5s |
| A4 | Process start (cold only) | inference-api `main.py` lifespan | Starts `warmup.py` on a daemon thread: imports `strands_tools.calculator` (sympy), the agent factory modules, and builds one client each for `bedrock-runtime`, `bedrock-agentcore`, `dynamodb`, `s3` **on boto3's default session**. `/ping` answers immediately. | none (client construction only) | **Measured** container: `warmup step=boto:bedrock-runtime ms=6` after imports |
| A5 | Middleware | inference-api | Outermost `InvocationActivityMiddleware` (marks the container busy for the whole streamed body), CORS, `AgentCoreContextMiddleware` (workload token, callback URL, session id → contextvars), `StreamSafeGZipMiddleware` (forwards SSE headers immediately; stock GZip withheld them until the first token). Then `get_current_user_trusted` verifies the bearer. | — | ~ms |

**Two facts about A4 that matter later.** First, the warm-up parses service models into the
*default* boto3 session's loader. **Every fresh `boto3.Session()` re-parses** (**Measured**
on a laptop 2026-09-29: default-session second client 2ms; a client on a fresh session
150–170ms *every time*, for `bedrock-agentcore` and `bedrock-runtime` alike). The AgentCore
Memory SDK, `MemoryClient`, and Strands' `BedrockModel` all construct fresh sessions, so
today's warm-up does not reach them (§3 F2). Second, under V2 whatever the warm-up has
finished before the snapshot is taken is paid once per deploy, not once per session — and
whatever it has not is redone on every restore (`agentcore-runtime-v2.md` B2.2).

### 2B. Inside `POST /invocations`, before the stream opens

Every row here runs **before** the `StreamingResponse` returns, i.e. with no channel open
to the client. The `turn_prelude` log line and the `AgentCoreStack/TurnLatency` EMF record
carry one mark per stage; the mark column is the name to filter on.

Legend for IO: **D** DynamoDB, **S** S3, **M** AgentCore Memory, **B** Bedrock, **C**
tool catalog (DynamoDB through a per-process 60s `config_cache`, or a per-tool 10s
freshness cache), **R** RBAC (per-process TTL cache), **sync** = a synchronous boto3
call inside `async def`, which blocks the event loop for its round trip.

| # | Mark | What runs | IO | Warm (**Measured** post-#1198) |
|---|---|---|---|---|
| B1 | `preamble.ownership` | `load_session_meta`: **one** `SessionLookupIndex` query answering both "is this someone else's session" (→ 404) and "the caller's META row" (threaded as `session_meta` into everything below). | D ×1 sync | 6–7ms |
| B2 | `preamble.skills` | `_apply_enabled_skills_filter(await _resolve_accessible_skill_ids(...))` **only when** `enabled_skills` is non-empty (opt-in, D6). | R + skill catalog | ~0 (6–21 with skills) |
| B3 | *(unmarked)* | Model retirement resolve (`resolve_effective_model`, 60s catalog cache). Then the two **non-turn early exits**: `app_tool_call` and `app_context_update` (MCP Apps) build/reuse the agent with `cache_write=False` and return JSON. | C | ~0 |
| B4 | `preamble.files` | `pop_pending_attachments` (snapshot; a write only if a marker exists) → `_select_recovered_attachments` → per-message file cap → `resolve_files` (**one S3 GET per upload id**) → dedupe → `_partition_attachments` (inline / tabular / pptx / oversized) → inline byte budget → marker names. | S ×N | 1ms with no attachments |
| B5 | `preamble.session_state` | `ensure_session_metadata_exists` (a conditional `put_item` on a new session; snapshot short-circuit otherwise) → `clear_paused_turn`, `clear_pending_interrupts`, `clear_truncated_turn`, `clear_interrupted_turn` (all snapshot short-circuits; a write only when a marker exists) → `set_pending_attachments` (write if uploads) → **title task spawned** on a first turn (`generate_conversation_title`, Nova Micro in `to_thread`, persists in the background). | D 0–3 | 3–4ms |
| B6 | `preamble.quota` | `check_quota(session_total_cost=<from snapshot>)`: tier (cached), user cost summary (cached), session notice from the snapshot. Early return with a conversational message if exceeded. | cached | 5ms |
| B7 | *(unmarked)* | Retired-model denial (conversational). Model access RBAC check. `_load_user_settings` (**one GetItem**, feeds default model + personal instructions). | R, D ×1 sync | ~6ms |
| B8 | `rag` | **Agent turns only** (`rag_assistant_id`, not resume). In order: `_session_has_messages` (mention turns only); `get_session_metadata` (**a second read of the META row**, outside the PR-2 snapshot); `get_assistant_with_access_check` (assistants table + shares); `resolve_invocation_agent` (version snapshot); `mark_share_as_interacted` (**write**, non-owner); `bump_last_used_at` (**conditional write every turn**, throttled to one real bump/day) + `resume_inactive_policies`; project gate (project row); `resolve_agent_invocation` (bound tools/skills/model/memory, RBAC); **knowledge-base search** (embedding + vector query; the one wait here the answer genuinely needs); prompt composition; memory hydration (`to_thread`); `get_session_metadata` (**third read**) + `store_session_metadata` (**full-row write every bound turn**). Every DynamoDB call in `assistants/service.py` constructs its own `boto3.resource("dynamodb")` (14 sites) rather than using `apis.shared.aws_clients` — the ~47ms-per-construction cost the preamble spec removed from `metadata.py` is still paid here. | D ×4–6 sync, KB | 0 on plain chat; unmeasured on agent turns |
| B9 | *(unmarked)* | Active custom prompt (`resolve_active_prompt_text`: a prompts-table read when the turn or session selects one); personal instructions composed for plain turns. | D 0–1 | ~0 |
| B10 | *(in `tools`)* | **Single-flight lease** `acquire_session_lease` (one conditional update; 409 if held) and, on a resume, `seed_steer_queue`. | D ×1 sync | ~6ms |
| B11 | `tools` | Non-resume: model fallback (`_resolve_fallback_model`: settings → catalog default → RBAC re-check), `_resolve_model_settings` (catalog cache; admin bounds/locks), attachment auto-enable (`_session_has_tabular`: a DynamoDB query unless memoized; RBAC per spreadsheet tool id), always-on union (catalog snapshot), **injected tool builders** (spreadsheet / artifact / word / workspace / excel / powerpoint / account / memory — closures, no IO), `_build_document_tools` (`_session_has_documents`: a query unless memoized), cache-key material. Resume: `get_paused_turn` + an eager `get_agent` from the snapshot. | C, R, D 0–2 | 98–102ms warm, 196–244 cold — **never decomposed** |
| B12 | `stream_setup` | Citations list, the two generators wired, `StreamingResponse` returned. Headers flush here. | — | ~1ms |

**`agent_build` is not in this table** because with `AGENT_PREPARING_PHASE_ENABLED`
(default on) it runs *inside* the stream (C1 below). With the flag off it runs eagerly
between B11 and B12 and the `preparing`/`prepared` frames are never sent.

### 2C. Inside the stream, before the first token

| # | Mark / clock | What runs | IO | Cost |
|---|---|---|---|---|
| C1 | `agent_build.*` | `preparing` frame → `get_agent` → `prepared` frame (`durationMs`) → `turn_prelude` emitted → lease heartbeat task started. **The build is synchronous and runs on the event loop** (`create_agent` is called without `await`), so for its whole duration nothing else in the process runs: the title task cannot deliver, `/ping` cannot answer, and a server-side timer around it cannot fire (agent-state-feedback PR-3 learned this on dev). Sub-stages in §2D. | see 2D | **Measured** 1–49ms warm (cache hit); **~3300ms cold** |
| C2 | *(unmarked)* | `stream_with_quota_warning`: `quota_warning` / `quota_session_notice` / `agent_notice` / `citation` frames; attachment guidance; **`_build_tabular_inventory`** (when spreadsheet tools are enabled: KB file listing + session file listing — DynamoDB/S3 reads); MCP App context drain; recovery / interruption / slash-command notes; `agent.stream_async(...)`. | D/S 0–2 | ~0 unless spreadsheet tools |
| C3 | *(unmarked)* | `ChatAgent.stream_async` → `PromptBuilder.build_prompt` → **`StreamCoordinator.stream_response` head of turn**: env vars, fingerprint/cancel reset, lease stamped, `apply_pending_compaction` (**re-reads compaction state from DynamoDB every turn**, #751), `apply_document_offload`, `reset_stale_interrupt_state`, **`_get_initial_message_count` → `session_manager.list_messages` → `ListEvents` over the whole session history**, just for `len()` (§3 F5), display-text arm, MCP Apps broker subscribe. | D ×1, **M ×1 (full history)** | unmeasured; grows with history |
| C4 | coordinator `stream_start_time` | `agent.stream_async(prompt)` through `process_agent_stream` and the 100ms `_merge_agent_status` poll (which also polls the title). Strands `MessageAddedEvent` → SDK `append_message` (`CreateEvent`, `to_thread`) **and `retrieve_customer_context`**: up to two `RetrieveMemoryRecords` (preferences + facts namespaces) in a `ThreadPoolExecutor`, awaited before the model call, one attempt each with a bounded timeout (`memory_retrieval_timeout_seconds`). `BeforeInvocationEvent` / `BeforeModelCallEvent` hooks (status, attribution capture, fingerprint, ledger, census). `count_tokens` is local (`native_projection=False`). `ConverseStream` opens. | M ×1–3, B | unmeasured server-side except the model call |
| C5 | `first_token_time` | First `contentBlockDelta` with text. The persisted `time_to_first_token` field measures **C4 → C5 only** — it is a *model* TTFT and is blind to everything above it (preamble spec PR-1b). | — | — |
| C6 | — | app-api relays chunks (keepalive comments during silence); the SPA renders. | — | — |

### 2D. Inside the build (`get_agent` → `create_agent`, a cache miss)

| Sub-stage | What runs | IO | Cold (**Measured** 2026-09-19) |
|---|---|---|---|
| *(pre-mark)* | `get_freshness_hash(enabled_tools)`: per-tool `updated_at`, 10s TTL, **one `GetItem` per enabled tool on a cold cache**; skills freshness; cache key; hit → return. | C ×N | in `tools` (B11) |
| `prompt` | `ModelConfig.from_params`, `RetryConfig.from_env`, `SystemPromptBuilder`. | — | 160 |
| `registry` | `create_default_registry` (imports; sympy behind `calculator`), `ToolFilter`, `_register_external_mcp_tools`: a `ThreadPoolExecutor` + fresh event loop + `asyncio.run`, **one `get_tool` GetItem per enabled tool** (uncached; `repository.get_tool` is a direct `get_item`). | C ×N | 50 |
| `session_mgr` | `SessionFactory.create_session_manager`: `load_memory_config`; **`_discover_strategy_ids`** (`MemoryClient(region)` → 2 clients on a fresh session, then a control-plane `get_memory_strategies` call; `lru_cache` per process, so **once per fresh process = once per conversation**); retrieval config; `TurnBasedSessionManager(...)` → SDK ctor builds **`MemoryClient()` (fresh session + 2 clients) and then a second fresh session + 2 clients that replace the first pair**; Strands `RepositorySessionManager.__init__` → `read_session` (**1 `ListEvents`, 2 on a new session** via the legacy fallback) → `create_session` (**1 `CreateEvent`** on a new session). | 4 parses, M ×2–3 | 616–830 |
| `tools` | `filter_tools_extended`; gateway: `_expand_gateway_tool_ids` (executor + `asyncio.run`, `get_tool` per gateway id) and `get_gateway_client_if_enabled` (client object; started later by Strands); external MCP: capture contextvars, executor + `asyncio.run` → `load_external_tools`: `get_tool` per server, `create_external_mcp_client`, **`await client.load_tools()` per server, serially** = `MCPClient.start()` (a background thread with its own loop, HTTP `initialize`) + `tools/list`; OAuth pre-flight recovery; `extra_tools` appended. | C ×N, MCP ×servers | **2039** (62% of the build) with **one** server on dev — undecomposed |
| `hooks` | Ten hooks constructed (no IO). | — | 0 |
| `plugins` | Skills runtime (skill records if any), `build_tool_result_offloader`. | 0–1 | 13 |
| `finalize` (#1377 splits it into `strands_agent` + `finalize`) | `AgentFactory.create_agent`: `CountTokensBedrockModel` → Strands `BedrockModel` → **fresh `boto3.Session()` + `bedrock-runtime` client (another parse)**; `Agent(...)`: tool registry `process_tools` (MCP clients register as consumers; `load_tools` is a no-op because the pre-flight primed the cache); `AgentInitializedEvent` → `TurnBasedSessionManager.initialize` → SDK `read_agent` (**1–2 `ListEvents`**, skipped on a new session) → `list_messages` (**1 `ListEvents`, `MAX_FETCH_ALL`**) → converter → document-byte strip → sanitize → compaction (`_load_compaction_state`: **1 DynamoDB GSI read**; possibly `_retrieve_session_summaries`) → pairing repair. New session: `create_agent` `CreateEvent`. Then `_adopt_session_conversation`, snapshot stamping, cache insert. | 1 parse, M ×2–3, D ×1 | 194 |

Sum of a measured cold build: 3286ms of a 4131ms prelude. Everything in 2D runs
**serially on one thread**, and the two big network stages — the MCP pre-flight and the
Memory session read/restore — do not depend on each other.

---

## 3. What the map exposes

**F1. The largest cold number is still a black box (Read).** `agent_build.tools` is
2039ms with one external MCP server, and the preamble spec's PR-5 (split it) is not
started. PR #1377 adds `session_mgr_clients` and `strands_agent` marks but leaves `tools`
whole. Nothing below should be built on a guess about what is inside it: it may be the MCP
`initialize` handshake against a cold Lambda, the two thread-pool hops with fresh event
loops, TLS setup, or the per-tool catalog reads. PR-5 comes first.

**F2. The build parses the same service models four to seven times, and the warm-up
parses them into a session nobody on this path uses (Measured on a laptop, mechanism
Read).** `_discover_strategy_ids` builds a `MemoryClient` (2 parses); the SDK constructor
builds one (2 parses) and throws it away for a second pair (2 parses); `BedrockModel`
builds its own session (1 parse). Each parse is ~150ms on a laptop and client
construction on the container measured ~30× a laptop in the preamble spec. `warmup.py`
builds `bedrock-agentcore` and `bedrock-runtime` clients on the **default** session, which
none of those constructors use. PR #1377's `shared_clients` arm fixes the SDK's pair —
lazily, on the first build, so a first turn still pays two parses plus the strategy-id
call — and does not touch `_discover_strategy_ids` or `BedrockModel`.

**F3. The build is serial where it could overlap (Read).** `session_mgr` (network:
read/create session; ~0.6–0.8s) and `tools` (network: MCP pre-flight; ~2s) are
independent, and the restore in `finalize` (network: `read_agent` + `list_messages`) is
independent of both. On an agent turn the knowledge-base search (B8) is independent of the
build too. Today each waits for the previous one. PR #1377's off-loop arm moves the whole
serial build to a worker thread; it does not overlap any of these with each other.

**F4. Agent turns carry writes and duplicate reads on the critical path (Read).** B8
reads the META row twice more (PR-2's snapshot covers only the preamble), writes it once
in full, does a conditional write for `lastUsedAt` on every turn, and marks shares on
every non-owner turn — all awaited before the stream opens, and each constructing its own
DynamoDB resource. None of them changes what the model is told this turn.

**F5. Every turn fetches the whole session history a second time to count it (Read; cost Measured 2026-10-02, §5 P4).**
`StreamCoordinator._get_initial_message_count` prefers `session_manager.list_messages`
over the maintained `message_count`, "because metadata retrieval uses global indices across
all agents' messages" (voice + text). For a `TurnBasedSessionManager` that is one
`ListEvents` with `MAX_FETCH_ALL` on the head of **every** turn, cache hit or not, and its
cost grows with the conversation. It sits in the gap between `prepared` and the first
`thinking` that the agent-state-feedback epic observed and could not explain.

**F6. The clock stops at the agent (Read).** `turn_prelude` ends at C1. C2–C4 (inventory,
head-of-turn compaction re-read, the history count, LTM retrieval, the hooks) are between
`prepared` and `thinking` and no server-side number covers them; the persisted
`time_to_first_token` starts after them. A first token that moved because of C3 would be
invisible to every dashboard we have.

**F7. Per-tool catalog reads are paid once per build per tool, twice (Read).**
`get_freshness_hash` and `_register_external_mcp_tools` each do a `get_item` per enabled
tool (the first on a 10s TTL, the second uncached); `load_external_tools` and
`_expand_gateway_tool_ids` read again per server. The whole catalog is already in the 60s
`config_cache`. Small per read with a cached client, but it is N×4 round trips per cold
build and every one is `sync` inside `async`.

**F8. Three sync→async bridges, each a thread and a fresh event loop (Read).**
`_register_external_mcp_tools`, `_expand_gateway_tool_ids` and the external MCP load each
spin a `ThreadPoolExecutor` to `asyncio.run` a coroutine, because the constructor is
synchronous and the loop is busy. Correct, and the reason the build cannot be `await`ed
piecemeal today. Any restructuring of the build should collapse these into one deliberate
boundary rather than three incidental ones.

**F9. What V2 changes about all of the above (Read from the V2 spec; Hypothesis on
timing).** Under V2 the cold start (A3) drops to ~2s flat and *every* first turn is a
restore. Process-start work (A4) is paid once per snapshot. So: anything static that is
computed at start (strategy ids, parsed service models, imported modules) becomes free per
session if it finishes before the snapshot; anything that opens a connection at start is a
dead socket in every restore (§B2.3 of the V2 spec); anything time-bounded pre-populated at
start (TTL caches on a monotonic clock) is a stale read after restore. The idle-clock
origin in `runtime_health.py` is the known hazard. The prewarm-on-intent idea (§5a) is the
one lever that hides the build itself, and it is V2-only for cost reasons.

**F10. The route handler is where the ambiguity lives (Read).** Most of the "how does a
turn work" questions above were answered by reading a 2,200-line function whose phases are
separated by comments and whose two generators close over its locals. The path is *not*
badly designed — every stage has a reason and most have a spec — but it is not legible.
§7 starts on that.

---

## 4. PR #1377, assessed

**What it does well.** It measured on dev before proposing, found the real mechanism (the
SDK's double client construction), noticed that every conversation is its own Runtime
process (so laptop warm-process numbers do not transfer), wrote the decision rule before
the data, and shipped everything **off by default** behind one flag with an interleaved,
session-stratified A/B and a script that joins client and server timings. That is exactly
the discipline the preamble spec asked for. The instrumentation (`session_mgr_clients`,
`strands_agent`, `buildArm`, `processBuilds`, the experiment script) has no cost on the
control arm and should merge regardless of the arms' fate.

**Where it lands short, by component.**

*Shared clients (`memory_shared_clients_enabled`).* Right target (F2), incomplete fix:

- The shared session is created **lazily by the first build**, so the first turn of every
  conversation — the only turn the A/B is about — still pays two parses plus the
  strategy-id round trip. The session, its two clients, and `_discover_strategy_ids` belong
  in `warmup.py`, on the same daemon thread, so a first turn finds them built. On V2 that
  also puts them in the snapshot. (Build the *clients*; make no calls except the one
  strategy-id read, which is static config — see P2 for the V2 caveat.)
- It leaves `_discover_strategy_ids`'s own `MemoryClient` and `BedrockModel`'s fresh
  session alone. Both take a `boto3_session` / `boto_session` argument today (verified in
  the pinned SDKs), so the same shared session closes them with no monkeypatching.
- The mechanism — rebinding `MemoryClient` inside the SDK module, gated by a contextvar
  set for the duration of one constructor — is the only seam the SDK offers for the
  throwaway client, and the PR pins it with a test that fails if an upgrade stops going
  through it. Acceptable, but it is a workaround for a one-line upstream bug
  (`MemoryClient(region_name=region_name, boto3_session=boto_session)`). File that
  upstream; the rebinding should have an end date.

*Off-loop build (`agent_build_off_loop_enabled`).* This arm carries all of the PR's risk
(the integration lock, the singleton lock, consumer pins, process-wide build
serialization) and its stated payoff is the title landing before `prepared`. Two things
to be clear about:

- **Moving a synchronous build to a thread does not make it faster.** CPU-bound parts
  contend on the GIL; network-bound parts take the same time. By itself the arm cannot
  reduce TTFT, and the A/B's TTFT comparison will most likely read as noise.
- **What it actually buys is concurrency on the loop during the build**: the title
  (measured), `/ping` (never measured — today a 3s build blocks the health poll; nothing
  has failed because of it, and V2's platform may or may not be as tolerant), and — the
  valuable one — the ability to start an agent turn's knowledge-base search (and anything
  else independent) *before* the build and have it finish *during* it. The A/B as designed
  measures the title only, and on plain chat turns, so the arm's real value is not on the
  scorecard.

**Recommendation** (applied to #1377 on 2026-09-30; kept here as the record of why)**.**

1. Merge the instrumentation and the `shared_clients` arm now, amended so the shared
   session, its clients and the strategy ids are built at warm-up (P2). Keep the arm behind
   the flag until the dev A/B reads; the decision rule in the PR stands.
2. Keep the off-loop arm in the A/B, but decide it on the right question. Either extend the
   experiment to agent turns with a knowledge base and start the KB search ahead of the
   build (P3b, small), or accept the PR's own rule and expect to remove the arm and its
   MCP hardening. Do not ship the hardening for a title.
3. Do not let #1377 be the vehicle for F1/F3. Split `tools` first (P1); the overlap that
   actually shortens a cold build (P3a) is a different, smaller change inside the
   constructor, and it does not need any of the off-loop machinery.

---

## 5. The plan

Ordered so every change has a measured before and after. Each item names its V2 posture
because the migration is coming and nothing here should have to be undone for it.

### P0 — Land the measurement in #1377, run the A/B, decide `shared_clients`

Unchanged from the PR: `AGENT_BUILD_EXPERIMENT=ab` on the dev Runtime out of band,
`experiment_agent_build_arms.py --per-arm 15 --cleanup`, the decision rule in the spec.
Amend the arm per §4 before the run, or the first-turn number will understate it.

### P1 — Close the two measurement gaps (F1, F6)

**Status (2026-10-01): P1a shipped (#1396) and read on dev.** Sub-stages are
`agent_build.tools.{filter,gateway,mcp,extra}`. `tools.catalog` and
`tools.mcp_preflight` are not separate stages: both happen per server inside the one
executor hop, and a mark cannot be taken there, so `tools.mcp` times the hop and the
`mcpServers` log property carries each server's `{id, outcome, catalogMs, preflightMs,
totalMs}` (`outcome` ∈ `cached`, `loaded`, `recovered`, `dropped`, `skipped`, `error`).
`tools.mcp` minus the servers' `totalMs` is the hop's own thread and event-loop startup.
`groups` now sums every dotted prefix, so `agent_build.tools` and `AgentBuildToolsMs`
survive the split. The catalog lookups that classify external MCP ids happen earlier, in
`agent_build.registry`.

**Dev readout (2026-10-01, one cold first turn, `sharedSession: true`, `processBuilds: 1`).**
`agent_build.tools` was **62ms**, not the 2039ms §3 F1 was written against: `filter`,
`gateway` and `extra` 0, `mcp` 62 — the one server (`hello_world`) took 4ms of catalog
read and 54ms of pre-flight before being dropped on its standing 403, and the executor
hop's own startup was 3ms. Acceptance met: the sub-stages sum to the group exactly and
one owns all of it. The 2039ms most likely predates P2's shared session (a cold build
parsed service models several times over), but it was never decomposed, so that is a
Hypothesis. **Caveat:** dev's only MCP server fails fast, so a cold `initialize` +
`tools/list` against a *working* server is still unmeasured; 62ms is a floor for that
case, not a typical value. The rest of that turn, for scale: prelude 1337ms = preamble
464 (`skills` 179, `quota` 145, `files` 77) + `rag` 67 + route `tools` 220 +
`agent_build` 577 (`session_mgr` 219, `finalize` 102, `prompt` 87, `registry` 64, `tools`
62, `plugins` 31).

*P1a. Split `agent_build.tools`* (the preamble spec's PR-5) into `tools.filter`,
`tools.catalog` (the per-tool reads), `tools.gateway`, `tools.mcp_preflight` and
`tools.extra`, with `groups.agent_build` still summing. Marks are taken on the calling
thread around each executor hop, never inside it (contextvars do not cross the pool). Add a
per-server `ms` list as a log property of the MCP stage so one slow server is
distinguishable from many.

**Status (2026-10-01): P1b shipped (#1397) and read on dev** (readout below). The clock is the same
`TurnPrelude`, passed to `ChatAgent.stream_async(turn_clock=...)` and on to the
coordinator. Stages after `turn_prelude`: `head_of_turn.handoff` (the route's generator
wiring, the `prepared` frame, quota warnings, the prompt build), `head_of_turn.compaction`
(compaction re-read, document offload, the per-turn resets), `head_of_turn.history_count`
(the `ListEvents` of F5), `head_of_turn.rest`, then `pre_model` and `model`. The boundary
between those two is `AgentStatusHook.first_model_call_at`, a `perf_counter()` stamp at
the turn's first `BeforeModelCallEvent` (stamped whether or not narration is on), closed
on the clock with `mark_at`. `ContextAttributionHook` is registered before the status hook
and so counts as `pre_model`; census, ledger and fingerprint are after it and count as
`model`. Without a stamp the gap is reported whole, as `pre_model_and_model`. "First
token" is the first model output of any kind — text, reasoning, or a tool call's first
block — since a tool-first turn's first visible output is the tool call.

Two departures from the text below. The numbers ride a **second line**,
`turn_first_token` (joined to `turn_prelude` by `sessionId`, carrying `firstTokenMs`,
`preludeTotalMs` and only the post-prelude stages), rather than the first line held
until a token: a held line is lost on every turn that never produces a token (an error,
a Stop, an interrupt), which are the turns most worth reading. And the line and its EMF
record (`FirstTokenMs`, `HeadOfTurnMs`, `PreModelMs`, `ModelMs`, …) are written on the
coordinator's next pass, **after** the first token has been yielded — a log write in
front of the token would be the latency being measured.

**Dev readout (2026-10-01, Haiku 4.5, one new conversation: cold first turn A, warm second
turn B).** Client columns are a passive `fetch` wrapper in the in-app browser (send →
first `content_block` frame). The server's first-token time is the sum of the stages:
#1397 read `firstTokenMs` at emit time, one event late, and overstated it by 131ms and
182ms here — fixed in the follow-up to read it at the `model` mark.

| | A (cold) | B (warm) |
|---|---|---|
| prelude (`turn_prelude.totalMs`) | 1263 | 229 |
| `head_of_turn` (handoff / compaction / history_count) | 157 (4 / 67 / 86) | 83 (4 / 5 / 74) |
| `pre_model` | 664 | 450 |
| `model` | 942 | 925 |
| **server first token** | **3026** | **1687** |
| client send → first content frame | 4264 | 2119 |
| hop (client − server: app-api, Runtime routing, cold start) | 1238 | 432 |

What `pre_model` is, from the runtime log on turn B — three network calls **in series**:
the user message's `CreateEvent` (~110ms), a second `CreateEvent` the Memory SDK logs as
`Created agent: default … with event` after every message (~107ms), then the two LTM
`RetrieveMemoryRecords` (~230ms warm, ~454ms cold, the namespaces finishing ~117ms apart
on the cold turn). LTM retrieval needs only the user's text, not either write, so the
writes and the retrieval need not be serial. `pre_model` is the largest stage we own on a
warm turn (450 of 1687ms), and it is paid on **every** turn, cold or warm.

`history_count` is 74–86ms on a two-message conversation (one `ListEvents` page). It grows
with history: ~2.25ms per stored event plus a step per 100-event page, ~390ms at 100+
events (measured 2026-10-02, §5 P4). `model` (~930ms) is Bedrock's own time to first token on a ~13k-token
prefix and is not ours to move here.

*P1b. Extend the clock to the first token.* Add `head_of_turn` (C3: compaction re-read,
history count, offload) and `pre_model` (C4: LTM retrieval + hooks, up to the model call)
marks, recorded by the coordinator on the same `TurnPrelude` (thread it through
`stream_async` like `turn_started_at` already is), and stamp `firstTokenMs` from handler
entry on the emitted line. `PreludeTotalMs` stays what it is; the new number is
`FirstTokenMs`. This is what makes P4/P5 provable.

*Tests.* `test_turn_timing.py`: new marks render, group sums hold, metric names derive.
Cost: perf_counter calls only. V2: none.

### P2 — Warm the right things, once (F2)

**Status (2026-09-30): built in PR #1377 as narrowed** — the off-loop arm was withdrawn, the shared session lives in `apis/shared/aws_clients.py`, warm-up builds its three clients and primes the strategy ids unconditionally, and the SDK session manager, `_discover_strategy_ids` and `BedrockModel` all take it on the `shared_clients` arm. The strategy-id cache is keyed by arm so control's first turn is unchanged. What remains of P2 is P0: the dev A/B decides whether the arm ships default-on.

- One process-wide `boto3.Session` in `apis/shared/aws_clients.py` (it already owns the
  client cache); warm-up builds `bedrock-agentcore`, `bedrock-agentcore-control` and
  `bedrock-runtime` clients on **it**.
- `SessionFactory` hands that session to the SDK (`boto_session=`), to
  `_discover_strategy_ids` (`MemoryClient(boto3_session=)`), and `ModelConfig.to_bedrock_config`
  passes it to `BedrockModel` (`boto_session=`; drop `region_name` when doing so — Strands
  rejects both). The `_SharedSessionMemoryClient` rebinding from #1377 covers the SDK's
  throwaway client until upstream takes the one-line fix.
- Warm-up calls `_discover_strategy_ids` once. It is a control-plane read of static
  configuration; on V2 that puts the ids in the snapshot. **Caveat:** it opens a
  connection. Either accept that the pool may hold one idle socket at snapshot time
  (botocore retries connection errors on this idempotent call), or take the ids from the
  environment instead — the memory construct could export them, but `CfnMemory` exposes no
  strategy-id attributes today, so that is a CDK custom resource, not a one-liner. Start
  with the warm-up call and watch the first restore's logs.
- Expected: `session_mgr` drops by the four parses it no longer does; `finalize` by one.
  Laptop arithmetic says ~0.6–0.9s of a fresh process's first build; the container ratio is
  unknown until measured. Warm turns: nothing (the agent is cached).

*Tests.* Warm-up builds each client once on the shared session (patch `boto3.Session`);
the factory passes the shared session (assert on the SDK ctor kwargs); `BedrockModel`
receives `boto_session` and no `region_name`; `reset_cached_clients` also resets the
session (the moto trap). V2: **positive** — all of it is snapshot-friendly, except the one
connection, which is the thing to watch.

### P3 — Overlap the build's independent IO (F3) — the real cold-turn lever

**Re-sized 2026-10-01 by the P1a readout: do not build P3a as written.** It was sized as
`max(2039, 830)` instead of `2039 + 830`. Measured on dev with P2 shipped, `tools` is 62ms
and `session_mgr` 219ms, so overlapping them saves at most ~60ms, and less once the second
thread's startup is paid. It returns only if a dev turn against a *working* MCP server
shows `tools.mcp` in the hundreds of milliseconds, or a deployment carries several
servers. P3b is unaffected, and is sized by `rag` on a KB agent's first turn. The next
decision should be made on P1b's `FirstTokenMs` breakdown, not on the build.

**That condition is now met (dev, 2026-10-02).** The first turn of a KB agent carrying two
external MCP servers (both Lambda function URLs, OIDC-forwarded) spent `turn_prelude.totalMs`
24276ms, of which `agent_build.tools.mcp` was **21543ms**: `mcpServers` =
`student_myboisestate` pre-flight 9462ms, then `class_search` 12058ms, `outcome: loaded`
for both. Nearly all of each is the first `initialize` POST (9.2s and 11.8s to its 200);
the requests after it took ~100–130ms, which reads as a Lambda cold start on an idle dev
environment (Hypothesis — prod traffic may keep them warm). `session_mgr` was 57ms, so
P3a *as written* (session manager ‖ tools) still buys nothing; the lever is its second
half — **load the per-server pre-flights concurrently**, merged in catalog order. On this
turn that is `max(9474, 12066)` instead of the sum, ~9.5s. Lambda warmth (provisioned
concurrency or a keep-warm on the servers we own) addresses the remaining ~12s and is a
separate, cost-bearing decision. `rag` on the same turn was 1576ms (P3b's size).

**Shipped (#1406) and read on dev (2026-10-02, runtime v521, image `817330dcd50b7bbd`).**
`ExternalMCPIntegration.load_external_tools` now builds each server in its own coroutine
(`_load_server`) and, with more than one, gathers them with each pre-flight on a worker
thread (`_preflight(off_loop=True)` → `asyncio.to_thread(asyncio.run, client.load_tools())`).
A plain `gather` would not have overlapped anything: Strands' `MCPClient.load_tools()` is
a coroutine whose body calls the synchronous `start()`, which blocks on the `initialize`
handshake. `gather` returns in input order, so clients — and the tool order in
`toolConfig` — are in catalog order; a lone server keeps the inline call. Kill switch
`MCP_PARALLEL_PREFLIGHT_ENABLED` (runtime-only, default on). Same agent, new conversation
each time:

| | `student_myboisestate` | `class_search` | sum | **`tools.mcp`** | `turn_prelude.totalMs` |
|---|---|---|---|---|---|
| before (serial), cold, 15:27 | 9474 | 12066 | 21540 | 21543 | 24276 |
| after, cold (~50 min idle), 16:18 | 2313 | 1894 | 4207 | **2316** | 4909 |
| after, warm, 16:21 | 384 | 464 | 848 | **473** | 2986 |

`tools.mcp` now tracks the slowest server, not the sum: ~1.9s saved on the cold sample and
~375ms warm. The servers were nowhere near as cold the second time (2.3s and 1.9s against
9.5s and 12s), so the 21.5s → 2.3s drop is mostly that; the part this change owns is the
sum-to-max gap, which on the first readout's numbers would have been ~9.5s. Both builds
produced the same `toolConfigHash` (`b12c22b73956…`) and `systemPromptHash`, so tool order
did not move. The cold turn called `search_classes` three times and answered from it.
With MCP loading reduced to the slowest server, `rag` (1404–1485ms) is now the largest
stage on this agent's first turn — P3b's case.

*P3a. Inside the constructor, no async plumbing.* In `BaseAgent.__init__`, `session_mgr`
and `tools` do not depend on each other (hooks, which take the session manager, are built
after both). Run them on two threads of one `ThreadPoolExecutor` and join — the same
mechanism the MCP load already uses, applied one level up. Tool order stays deterministic
(one thread builds one list). Marks: take `agent_build.session_mgr_ms` and
`agent_build.tools_ms` as properties from each thread's own clock and one wall-time stage
`agent_build.parallel` on the calling thread, so the sum-of-stages invariant the
unaccounted-time widget depends on still holds. Expected on the measured cold build:
`max(2039, 830)` instead of `2039 + 830`, roughly **−0.8s**. Then, if P1a shows the MCP
pre-flight is the bulk of `tools`, the per-server loads run on the pool too, merged in
catalog order (the preamble spec's warning: the *merge* must be deterministic because tool
order is the prompt-cache prefix).

The restore (`initialize`, inside `Agent(...)`) can join the overlap later by prefetching
its three `ListEvents` on a third thread and handing the SDK the results for the one
`initialize` that follows; that is a second step, after P3a is measured.

*P3b. On agent turns, start the knowledge-base search before the build.* The build closure
does not read `context_chunks`; only the stream does, when it composes `final_message`.
Submit the search to a thread before the deferred build starts and await its future in
`stream_with_quota_warning`. This is the overlap #1377's off-loop arm would enable "for
free"; without that arm it needs the search to be on a thread, which it can be. Keep
citations and augmentation byte-identical (order of chunks, cap) — they land in the
persisted user message, which is the cacheable prefix on every later turn.

*Tests.* P3a: a fake session factory and a fake tool loader that each sleep; assert the
build's wall time is the max not the sum, that `tools` order is unchanged across runs
(`test_prompt_cache_determinism.py` already pins order), and that a failure in either thread
surfaces as the build's exception with the other thread joined (no orphaned MCP client:
`consumer_pin` semantics from #1377 apply here too). P3b: the KB search is started before
`preparing` is emitted and its result reaches `final_message` unchanged; a failed search
still yields the unaugmented message (today's fail-open). Dev: fetch-tee harness, first
turn with an MCP tool enabled and a KB agent; compare `agent_build` group and
`FirstTokenMs` before/after. V2: neutral — threads, no sockets at start.

### P4 — Take the bookkeeping off the critical path (F4, F5, F7)

*P4a. Overlap the long-term-memory lookup with the message writes (shipped #1400, read
on dev 2026-10-01).*

**Dev readout (2026-10-01, Haiku 4.5, one new conversation: cold first turn A, warm second
turn B; same recipe as the P1b readout, with #1398 reading `firstTokenMs` at the `model`
mark).** Before is the P1b readout corrected by #1398 (its stage sums).

| | A (cold) before → after | B (warm) before → after |
|---|---|---|
| `pre_model` | 664 → **352** | 450 → **270** |
| prefetch `waitedMs` / `lookupMs` | — / 338 | — / 268 |
| `head_of_turn` (handoff / compaction / history_count) | 157 → 118 (5 / 56 / 57) | 83 → 93 (6 / 4 / 83) |
| `model` | 942 → 810 | 925 → 867 |
| **server first token** | **3026 → 2589** | **1687 → 1451** |
| client send → first content frame | 4264 → 4543 | 2119 → 1898 |

`pre_model` fell by about the lookup it stopped waiting for. On the warm turn the lookup
finished inside the two writes (`waitedMs=0`), so what is left of `pre_model` is the
SDK's two `CreateEvent`s — the user message's (~110–170ms) and `sync_agent`'s (~100ms,
see *Not done here*). On the cold turn the lookup outlasted the writes by 141ms. The cold
client number rose on one sample while the server number fell 437ms; the difference is
the hop and the prelude (1263 → 1307), which this change does not touch. `firstTokenMs`
now equals the stage sums to within 2ms. Byte check, in AgentCore Memory `ListEvents`:
the persisted user messages carry one block and no `<user_context>`, while the cold turn
inserted three records into the live message — the persisted/live split is unchanged.

The P1b readout found `pre_model` — the largest stage we own on a warm turn, 450 of
1687ms, and paid on every turn — to be three network calls in series, all on
`MessageAddedEvent` for the user's message: the SDK's persist callback (`append_message`'s
`CreateEvent`, ~110ms, then `sync_agent`'s `CreateEvent`, ~107ms) and then
`retrieve_customer_context` (two `RetrieveMemoryRecords`, ~230ms warm, ~454ms cold).
The lookup needs only the user's text, not either write.

*The constraint that shapes it.* `retrieve_customer_context` prepends a `<user_context>`
block to the **live** user message, after the persist callback has written that message.
So the persisted message — what restore rebuilds history from on a rebuilt agent — has no
context block, and the live one does. That split is load-bearing for the prompt-cache
contract (restored history must be byte-stable), so running the two callbacks
concurrently is wrong: the insert could land while `append_message` is serializing the
same dict on its worker thread, and a context block would reach Memory on some turns and
not others.

*What it does instead.* `TurnBasedSessionManager.register_hooks` registers one sync
`MessageAddedEvent` callback **before** delegating to the SDK. Strands runs a hook's
callbacks one at a time in registration order (`invoke_callbacks_async`), so it runs
first: it checks the same eligibility the lookup does (last message is a user message
whose first block is text, retrieval configured, session not cancelled), snapshots the
query string, submits `_fetch_customer_context(query)` to a small process-wide pool and
returns. The SDK's persist callback then runs exactly as before. When the SDK calls
`retrieve_customer_context` — still after the persist — it takes the prefetched future
(only if it was started for this same message object; otherwise it fetches inline, as
before), waits for it, and inserts the block. Only the network round trip moves; the
insert happens at the same point in the sequence, so the persisted bytes and the live
bytes are what they were. Expected: `pre_model` falls by about `min(writes, lookup)` —
~200ms warm, more cold.

*Kill switch:* `MEMORY_RETRIEVAL_PREFETCH_ENABLED` (default on; only `"false"`
disables), runtime-only like `AGENT_BUILD_SHARED_SESSION_ENABLED`. Off, nothing is
prefetched and the lookup runs inline in `retrieve_customer_context`, as before.

*Tests.* Through a real Strands `HookRegistry` with the SDK's own async-mode wiring: the
lookup starts before `append_message` returns, the message `append_message` serialized
carries no context block while the live message does, and the result is byte-identical to
the switch-off path. Plus: prefetch is skipped for assistant and tool-result messages; a
prefetch for a different message is never applied; a failed or slow prefetch leaves the
turn without context rather than failing it. *Measure:* `pre_model` on a warm and a cold
dev turn against the P1b readout, and the `memory retrieval prefetch` line's `waitedMs`
(0 means the lookup finished inside the writes).

*Not done here:* `sync_agent` after every message is the SDK writing agent state that its
`AfterInvocationEvent` callback writes again at the end of the turn. Skipping the
per-message write would save ~107ms more, but it changes what the SDK persists if a turn
dies mid-way; it needs its own look.

- **`_get_initial_message_count`:** use the maintained `message_count` and drop the
  `ListEvents`. The global-index concern it cites is for mixed voice+text sessions; verify
  with `get_messages_from_cloud` whether the index still needs to be global (voice writes
  under a different agent id) and, if it does, count once at `initialize` and keep the
  count maintained rather than refetching the history every turn.

  **Measured (2026-10-02, dev, image `815293d3a627d57e`).** One disposable conversation
  (`exp-histcount-…`, Haiku 4.5, no tools) driven headless to 60 short plain turns, three
  seconds apart, every turn warm after the first; `head_of_turn.history_count` from each
  turn's `turn_first_token` line, against the events stored before that turn. A plain
  turn stores two events (user + assistant), ~0.5 KB each, so 50 turns is the first
  two-page read.

  | Events before the turn | Turns | `history_count` median (min–max) | share of `firstTokenMs` | share of what we own (`firstTokenMs` − `model`) |
  |---|---|---|---|---|
  | 0 (cold first turn) | 1 | 59 | 3% | 4% |
  | 1–19 | 8 | 92 (72–112) | 8% | 17% |
  | 20–39 | 10 | 120 (104–171) | 10% | 23% |
  | 40–59 | 10 | 167 (151–204) | 14% | 30% |
  | 60–79 | 10 | 219 (187–246) | 17% | 35% |
  | 80–99 | 10 | 266 (232–323) | 19% | 40% |
  | 100–122 (two pages) | 11 | **388** (363–450) | **26%** | **49%** |

  On one page it fits ~62ms + 2.25ms per event; the second page adds a round trip
  (~80ms above the per-event trend). It draws level with `pre_model` (~210–300ms here,
  flat) at ~60–79 events and passes it after that, which makes it the largest stage we own
  on any longer warm turn — and the only one that grows. **These are the cheap events.** The read pulls every payload
  (`includePayloads=True`) and converts them all for a `len()`, so bytes count too: a
  read-only replay of the same call from a laptop against 40 existing dev conversations
  took 150ms at 25 events / 30 KB, 300ms at 83 events / 206 KB, and 419ms at 50 events /
  2.6 MB (a document conversation). Tool turns store more events per turn than chat.

  **Recommendation: build it next.** The fix is to stop reading the history to count it — no change
  to anything the model sees, so the cacheable prefix is untouched. What the count feeds
  is the message index for per-message metadata (`message_index=initial_message_count`
  and the `+ 2*i + 1` arithmetic in the coordinator), so the open question is still the
  one above: whether that index must be global across voice and text agents. Verify it
  against `get_messages_from_cloud` before choosing between the maintained count and a
  count taken once at `initialize`; either removes the read from every warm turn.
  *Accept if:* `head_of_turn.history_count` ≈ 0 on a 100-event conversation, and the
  per-message metadata (cost, latency, citations) still lands on the right message after
  reload in a text-only and a mixed voice+text session.

  **Shipped (#1403) and read on dev (2026-10-02, runtime v519, image `86e94164a788488e`).**
  The count is the same global read, started on a worker at the head of the turn
  (`HistoryCount`, `agents/main_agent/streaming/history_count.py`) and counting only
  messages whose `created_at` precedes that instant; the displayText hook, the artifact
  anchor, the end-of-turn metadata and the interruption persistence each await it where
  they use it. The maintained per-instance `message_count` was rejected for the reason
  above it — the voice, `@`-mention and synthetic-message writers make it stale. Kill
  switch `HISTORY_COUNT_PREFETCH_ENABLED` (runtime-only, default on). Same recipe as the
  measurement: 15 more turns on the same conversation, 10 on a fresh one.

  | | `history_count` | `firstTokenMs` | what we own (`firstTokenMs` − `model`) | `pre_model` |
  |---|---|---|---|---|
  | before, 100–122 events (n=11) | 388 | 1560 | 791 | 225 |
  | after, 122–150 events (n=14) | **0** | **1092** | **395** | 236 |
  | after, fresh conversation (n=9) | 0 | 1043 | 403 | 229 |

  A 150-event conversation now costs what a fresh one does. The read itself still runs
  (`lookupMs` 409–635 on the long conversation, 62–109 on the fresh one — it now overlaps
  the writes and the model call), and `waitedMs` was 0 on all 25 turns. *Correctness:*
  every `count=` equalled the messages stored before the turn (120, 122, … 148; 0, 2, …
  18), although each read ran long past that turn's user-message write — the cutoff is
  doing the work. A read-only replay of the `GET /messages` join on both conversations put
  every cost row on an assistant message, none on a user message, none past the end of the
  history. *Augmented path:* two turns on a KB agent stored `displayText` at append time on
  user messages 0 and 2 with `waitedMs=0`, and the reload shows it on exactly those
  messages. Still unexercised: a mixed voice+text session.
- **B8 writes:** `mark_share_as_interacted`, `bump_last_used_at` + `resume_inactive_policies`,
  and the binding persistence `store_session_metadata` become fire-and-forget tasks (strong
  references held, like `_pending_title_writes`) or move to the coordinator's post-stream
  metadata write. Validation stays where it is. The binding write in particular must still
  land before the *next* turn's validation reads it; post-stream is early enough.
- **B8 reads:** thread `session_meta` (PR-2's snapshot) into the assistant block's two
  `get_session_metadata` calls; convert `assistants/service.py`'s 14 resource constructions
  to `get_dynamodb_table`.
- **Catalog reads:** `get_tool` served from the `config_cache` snapshot inside its TTL,
  falling through to `get_item` on a miss. Removes N×4 round trips per cold build.

*Tests.* Each deferred write still happens exactly once per turn and after `done` at the
latest (patch the repository, assert call order against the frames); a turn that errors
before the stream still binds; the snapshot-threaded reads behave as PR-2's did
(`test_a_snapshot_with_no_row_is_an_answer_not_a_cache_miss` pattern). V2: neutral.

### P5 — Runtime V2 track (F9)

In the order the V2 spec gives, with two additions from this map:

1. Deploy-script fix (B1: `platformVersion` in `ALLOWED`, post-update assert, CLI check).
2. Explicit `PlatformVersion` flag in CDK, always set.
3. Idle-clock re-stamp in `runtime_health.py` (B2.1) — plus **an audit of every
   `time.monotonic()`-keyed cache reachable at start** (tool freshness, `config_cache`,
   `oauth_token_cache`, RBAC): none may be pre-populated by warm-up. P2 deliberately warms
   clients and strategy ids, which are not time-bounded.
4. Dev A/B with `FirstTokenMs` (P1b) and the client-side send→first-delta gap, split cold
   vs warm; `get-agent-runtime` after `platform.yml` **and** after the next `backend.yml`.
5. Decide warm-up placement from the restore logs: if warm-up work lands in the snapshot,
   hold `/ping` until it completes so every restore inherits it.
6. **Prewarm on intent** (V2 spec §5a): a no-op `/invocations` action that takes no lease,
   writes no rows, charges no quota and makes no model call, fired from the SPA on composer
   focus with the staged session id. This is the only change that hides A3 + A4 + the first
   build from the user, and it is worth more than everything in P2–P4 on a cold turn. A
   later step pre-builds the agent for the current selection; keep it separate (selections
   change before send).

### P6 — Keep the room clean

- `count_tokens` stays local; the LTM retrieval stays bounded and one-attempt; the 100ms
  status poll stays. None of them are on this list because each was already measured and
  chosen.
- Every hook on `BeforeModelCallEvent` keeps capturing raw facts only (CLAUDE.md).
- Nothing in P2–P5 touches the prompt, `toolConfig` or restored history. If a change here
  ever needs to, it is a different spec.

---

## 6. Regression plan — for hand-off

Written so a less expensive model can run it without this document's context. Three
parts: invariants that must never move, the checklist per plan item, and the dev recipes
that settle timing claims. Run everything from the worktree with the main checkout's
interpreter:

```bash
cd <worktree>/backend
PYTHONPATH=$PWD/src AWS_EC2_METADATA_DISABLED=true \
  <main checkout>/backend/.venv/bin/python -m pytest tests/ -q
```

`PYTHONPATH` is not optional: without it the interpreter imports `develop`'s code from the
main checkout and a green run proves nothing about the branch. Verify once with
`python -c "import apis.shared; print(apis.shared.__file__)"`. The suite's socket guard
fails any test that reaches the network; a new test that "passes" while hitting AWS is a
test whose assertion never ran.

### 6A. Invariants (assert before and after every change on this path)

| # | Invariant | How to check |
|---|---|---|
| I1 | **SSE frame order.** Head: `quota_warning?` → `quota_session_notice?` → `agent_notice?` → `citation*` → `message_start`. Deferred build: `agent_status{preparing}` first frame, `agent_status{prepared, durationMs}` before `message_start`, always paired. Tail: `message_stop` → interrupt events → `metadata` → `compaction?` → `done`. `session_title` may appear anywhere, at most once, never `"New Conversation"`. | `tests/routes/test_inference.py`, `tests/apis/inference_api/test_preparing_phase.py`, `TestRouteContract` (string pins in `routes.py`) |
| I2 | **Prompt-cache byte stability.** Same session, same configuration, consecutive turns: `toolConfigHash` and `systemPromptHash` identical; `historyHash` extends. Tool order in `toolConfig` is deterministic across builds. | `tests/agents/main_agent/test_prompt_cache_determinism.py`; on dev, `GET /admin/costs/sessions/{id}/calls` — consecutive rows with `cacheStatus=hit` and equal `toolConfigHash` |
| I3 | **One cache key per configuration, shared by all three callers.** The main turn, the resume (`is_resume=True`, `cache_write=False`, `memory_binding=snapshot.memory_binding`) and the MCP App dispatch (`cache_write=False`) compute the same key for the same session state. | `tests/apis/inference_api/test_get_agent_call_sites.py` (AST-pinned), `test_chat_service.py::test_resume_replay_from_snapshot_hits_same_cache_slot` |
| I4 | **Lease discipline.** Acquired exactly once per turn before the stream; released on every exit: stream end, cancellation, pre-stream `HTTPException`, pre-stream error; heartbeat cancelled first. | `tests/apis/inference_api/test_turn_lease_release.py`, `test_carried_steering.py` |
| I5 | **No session state on an agent instance.** `turn_lease`, `cancelled`, display-text arm, fingerprints, interrupt state are reset at the head of every turn; conversation list is aliased across instances; compaction state is re-read per turn. | `test_chat_service.py::test_second_cache_key_for_a_session_shares_the_conversation`, `tests/agents/main_agent/session/test_compaction_deferred_apply.py`, `test_turn_based_session_manager.py` |
| I6 | **Early exits stream, they do not 4xx.** Quota exceeded, retired model without successor, blocked agent binding, archived project: a conversational `message_start … done` with the metadata event, persisted via `persist_synthetic_messages`. Only ownership (404), model access (403), resume validation (400), duplicate turn (409), missing assistant (404/403) raise. | `tests/routes/test_inference.py`, `test_model_retirement_routes.py`, `test_project_harness_invocation.py` |
| I7 | **Attachment semantics.** CSV/XLSX never inline; PPTX never inline; oversized dropped with a note; per-message cap and byte budget applied before S3 reads; marker names follow attachment order; recovered attachments dropped when the turn carries its own. | `test_attachment_turn_guard.py` (37), `test_attachment_recovery.py`, `test_presentation_attachment_carveout.py`, `test_attachment_tool_autoenable.py` |
| I8 | **`turn_prelude` shape.** One line per agent turn with `stages`, `groups` (`preamble`, `agent_build`), `totalMs`, `isResume`, `deferredBuild`, `hasAssistant`; EMF metric names derived from stage names; disabled by `TURN_LATENCY_METRICS_ENABLED=false`. | `test_turn_timing.py` |
| I9 | **No network in tests.** | the socket guard in `tests/conftest.py` (fails at teardown) |
| I10 | **Warm-up never opens a connection** (until P2 deliberately adds the one strategy-id read). | `test_warmup.py` (`boto3.client` patched, assert no call methods invoked) |

### 6B. Per-change checklist

For each plan item, the cheaper model should: (1) run the named tests before touching
anything and record the count; (2) make the change; (3) run the same tests plus the new
ones; (4) run the full suite once; (5) run the dev recipe and paste the numbers into the
PR with the *before* numbers from the same recipe on the same day.

| Item | Unit tests to add | Existing tests to run | Dev recipe | Accept if |
|---|---|---|---|---|
| P1a split `tools` | marks appear in order; `groups.agent_build` sums; per-server ms list is a property | `test_turn_timing.py`, `test_preparing_phase.py` | R1 on a first turn with one MCP tool enabled | sub-stages sum to `tools` ±5ms; a named owner of ≥60% of it |
| P1b first-token clock | `head_of_turn`, `pre_model`, `firstTokenMs` on the line; `FirstTokenMs` metric | `test_turn_timing.py`, `tests/agents/main_agent/streaming/` | R1 + R2 on a warm and a cold turn | `firstTokenMs` ≈ client send→first-delta minus the A2/A3 hop |
| P2 shared session at warm-up | see §5 P2 | `test_warmup.py`, `test_session_factory*.py`, `tests/agents/main_agent/core/` | R1 on three first turns per arm | `session_mgr` + `finalize` down by ≥ the parses removed (≥300ms cold); warm unchanged |
| P3a parallel constructor | see §5 P3a | `test_prompt_cache_determinism.py`, `test_base_agent_external_registration.py`, `tests/agents/main_agent/integrations/` | R1, R3 | `groups.agent_build` cold ≈ max not sum; I2 holds across 5 turns |
| P3b KB search ahead of build | see §5 P3b | `tests/routes/test_inference.py` (assistant cases), `test_project_harness_invocation.py` | R1 on a KB agent's first turn | `rag` mark ≈ 0 and `FirstTokenMs` down by ≈ the old `rag` |
| P4 bookkeeping off the path | see §5 P4 | `tests/routes/test_assistants.py`, `tests/routes/test_sessions.py`, `test_agent_binding_scoped_tools_e2e.py` | R1 + R4 | `head_of_turn` and `rag` down; every write still observed in DynamoDB after the turn |
| P5 V2 | per the V2 spec | `test_runtime_health.py`, infra `jest` | R2 cold vs warm, `get-agent-runtime` twice | no 424s on first turns; `FirstTokenMs` cold down |

### 6C. Dev recipes

**R1 — server-side stages.** Log group from SSM `/<prefix>/inference-api/runtime-id`
(the group is `/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT`; the prefix-named
group returns zero rows, not an error). Then:

```bash
aws logs filter-log-events --log-group-name <group> --filter-pattern turn_prelude \
  --profile dev-ai --start-time <epoch ms> --query 'events[].message' --output text
```

Lines are wrapped in OTEL JSON with escaped quotes. Confirm the image tag in
`/<prefix>/inference-api/image-tag` changed before trusting a number as "after". A window
that spans a deploy mixes two populations: read min or individual lines, never the mean.

**R2 — client-side first token.** In the in-app browser signed in to dev, wrap
`window.fetch` to `tee()` the `/chat/stream` body and timestamp frames matching
`agent_status|session_title|content_block_delta|done` from the moment of the request (the
recipe in the agent-state-feedback and session-title memories). The gap between request
and the first `content_block_delta` is the number the user feels; subtract `PreludeTotalMs`
(or `FirstTokenMs` after P1b) from the same turn to get the A2/A3 hop.

**R3 — cache stability.** `GET /admin/costs/sessions/{id}/calls` for the session; on
turns 2..n, `cacheStatus` should be `hit` with equal `toolConfigHash` / `systemPromptHash`.
`partial_miss` or a changed hash after a build change means tool order or prompt bytes
moved.

**R4 — the writes still land.** After an agent turn: the session's META row carries
`preferences.assistantId`; the assistant's `lastUsedAt` is today (first turn of the day);
the share row is marked interacted (non-owner). One `get-item` each.

**R5 — the A/B.** `backend/scripts/experiment_agent_build_arms.py` (from #1377) with
`AGENT_BUILD_EXPERIMENT=ab` set on the dev Runtime out of band. Pass env vars explicitly;
zsh does not word-split `$var`.

### 6D. The end-to-end matrix (run once per release on dev)

For each row: send the turn, watch R2's frames, confirm the row's "expect", then reload the
page and confirm the conversation restores with the same content.

| Turn shape | Expect |
|---|---|
| First turn, plain chat | `preparing`/`prepared`, `session_title` before or during the answer, title persisted (R4) |
| Second turn, same session | no `preparing` label visible (build 0–40ms), `cacheStatus=hit` |
| Agent with KB, first turn | `citation` frames before `message_start`; answer uses the corpus; binding persisted |
| `@`-mention of an Agent in a plain thread | that turn runs the Agent; the next plain turn sees the mention's messages (#741) |
| Attach a PDF | inline; `[Attached files: …]` marker; `document_read` appears in tool list; card survives reload |
| Attach a CSV | diverted; Spreadsheet Analysis auto-enabled (granted role); guidance note; card survives reload |
| Attach a PPTX | diverted; PowerPoint tools note; card survives reload |
| Enable a skill and `/invoke` it | `<available_skills>` present; the directive line; the `skills` tool called |
| External MCP tool needing OAuth | `oauth_required` after `message_stop`; consent; resume finishes the same turn (I3) |
| `ask_user_question` | `user_question_required`; answer resumes; "Skip" resumes |
| Stop mid-answer | partial persisted; `interrupted_turn` marker; next turn carries the interruption note; no 409 on resend |
| Steer mid-turn (a tool turn) | `steering_applied` at the tool boundary, or the follow-up sent as the next turn |
| Continue after `max_tokens` | the answer continues, does not restart; Agent tools/skills intact |
| Quota exceeded (test tier) | conversational message, `quota_exceeded` event, persisted, no model call |
| Preview session (`preview-…`) | works; nothing in the sidebar; no lease |
| Duplicate send while streaming | 409 → "already streaming" (the SPA queues instead) |

---

## 7. Clarity refactors

The route's phases are now named functions with typed results, in the order the map
lists them, so the handler reads as the sequence §2B describes. Behaviour-preserving by
construction: each extraction moves a contiguous block, keeps every module-level seam the
tests patch (`_load_user_settings`, `_resolve_fallback_model`, `get_agent`,
`ensure_session_metadata_exists`, …) and every string the `TestRouteContract` and
call-site AST tests pin. See the PR that introduced this document for the list.

Next, in this order, each its own PR:

1. The assistant block (B8) into `resolve_agent_turn(...)` returning either a refusal to
   stream or the resolved instructions/overrides/citations, with its writes moved per P4.
2. The build's three sync→async bridges (F8) into one `run_blocking_async(coro)` helper
   with the contextvar capture in one place.
3. `stream_with_quota_warning` and `_guarded_stream` into a `TurnStream` object that owns
   the head frames, the build, the lease heartbeat and the title poll, so the handler ends
   at "return the stream".

Rules that keep this legible afterwards: a phase function takes what it reads and returns
what it produces (no closure over handler locals); a phase that can end the turn returns
the `StreamingResponse` or raises, it does not set a flag; every new stage gets a mark; and
a comment that explains *why* stays with the code it explains, not in this document.

---

## 8. Open questions

- What is inside `agent_build.tools` (P1a answers it).
- ~~What C3 costs on a long conversation~~ — measured 2026-10-02 (§5 P4): ~2.25ms per
  event plus a page step, ~390ms at 100+ events; off the critical path since #1403. The
  global read was kept (voice and `@`-mention writers), so the voice+text question no
  longer blocks anything.
- The container's ratio for service-model parsing (laptop ~150ms each).
- Whether AgentCore Runtime tolerates `/ping` being blocked for the length of a cold build
  on V2 as it evidently does on V1.
- Whether a `CfnMemory` custom resource is worth it to make strategy ids environment
  configuration instead of a start-up read.
