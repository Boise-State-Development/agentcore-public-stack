# Model fallback profiles

**Status:** Design. Nothing here is built yet.
**Scope:** A chat turn's model calls (the main agent that `AgentFactory.create_agent`
builds). api-converse, voice, the side-channel calls (titles, tool summaries),
compaction's summarizer, `document_extract` and the handoff brief are out of
phase 1 (§11).
**Refs:** `agents/main_agent/core/retry_strategy.py` and
`apis/shared/models/stream_output.py` (what is retried, and the "never after
visible output" rule); `apis/shared/rbac/model_access.py` (the one access
predicate); `apis/shared/models/retirement.py` (the closest existing "this row
points at another row" pattern); `docs/specs/turn-path-ttft.md` (the budget this
must not touch); branch `feature/provider-outage-message` (the outage reply this
builds on).

---

## The question this answers

> On 2026-11-13 production's `global.anthropic.claude-opus-5-5` returned
> `ServiceUnavailableException` on every call from about 16:15 to 23:45 UTC,
> while `us.anthropic.claude-opus-5-5` answered from the dev account. Could a
> turn try an alternate model id on its own when the provider fails, allowing
> for the price difference? And does that mean entering the model twice?

**Yes, and yes: it is a second catalog row, though the admin should not have to
type it.** An admin gives a managed model a `fallbackModelId` naming another
managed-model row. When a model call on the primary fails on the provider's
side *before any output reached the user*, the same request is sent to the
fallback, at once and without backoff. The fallback row carries its own prices,
so the call is metered at what it actually cost (Geo is about 10% above Global).
Its cost row says it was a fallback, so the one-off prompt-cache re-write it
causes is explained rather than booked as waste. In phase 1 a fallback must be
the **same model under another inference profile** (`global.` → `us.`). A short
in-process circuit breaker keeps a conversation on the fallback while the
primary is down and while the fallback's cache is warm. Nothing runs before the
first token on the happy path.

What the 2026-11-13 failure cost without this: each failed Opus turn spent 4
app retries × botocore attempts and **failed after 30–70 s**, and then the user
had to resend or switch models by hand. With a fallback configured, that turn
pays one failed primary attempt (seconds), then streams from `us.` Opus 5.5. The
next turns in that conversation skip the primary entirely while the breaker is
open (§5.3).

---

## 1. Why a fallback has to be a catalog row

Everything that prices or permits a model call keys on the model id and expects
**one catalog row per id**:

- **Pricing.** `get_model_by_model_id` (`apis/shared/costs/pricing_config.py`)
  scans the catalog for the row whose `model_id` matches. `get_model_pricing` and
  `create_pricing_snapshot` build on it. No row means the call prices to `None`.
- **Metering.** A call with usage that prices to `None` is stored at $0, counted
  against no quota, and fires `UnmeteredModelCall`.
- **Access.** `grants_model_access` refuses an id with no catalog row, for
  everyone and `*` holders included (the PR 2016 rule), because of the two bullets
  above.
- **Context window.** Model-relative compaction reads `maxInputTokens` off the
  row.

So "just try `us.` instead of `global.`" as a string rewrite inside the runtime
would either be refused or run free. The fallback is a row with its own
`modelId`, prices, cache rates and window, which the curated catalog already
ships: `curated-models.ts` carries Opus 5.5 as `us.anthropic.claude-opus-5-5`
at the Regional rates ($4.40 / $22.00, cache write $5.50, cache read $0.22).
Prod's `global.` row is that template edited down to Global rates ($4.00 /
$20.00 / $5.00 / $0.20).

**Entering the model twice is real but should be one click.** Phase 1's admin
form gets an **Add fallback route** action on a `global.*` row. It creates the
sibling `us.*` row from the curated template (the template's `pricingTier:
'regional'` rates, never a copy of the primary's Global rates), marks it
fallback-only, and sets the primary's `fallbackModelId`. The admin reviews and
saves, and nothing is typed.

## 2. Data model

Two fields on `ManagedModel` / `ManagedModelCreate` / `ManagedModelUpdate`
(`apis/shared/models/models.py`), mirrored in the SPA's `ManagedModel`:

| Field | On | Meaning |
|---|---|---|
| `fallbackModelId: Optional[str]` | the primary | `modelId` of the row a call switches to on a provider-side failure. |
| `fallbackOnly: bool = False` | the fallback | The row is a route, not a choice. It is not offered in any picker and is refused when named directly (§3). |

### 2.1 Why a new `fallbackOnly` flag and not `isFeatured` or `status`

- **`isFeatured: false` only demotes.** The SPA's `pickableModels`
  (`session/services/model/model.service.ts`) is every non-retiring model, and
  `moreModels` is exactly the `isFeatured === false` ones. A demoted `us.` Opus
  would sit under "More models" next to the real one, at a higher price, and
  users would pick it.
- **`status` is lifecycle.** `ModelStatus` (`active` / `deprecated` / `retired`)
  drives the retirement redirect and the pickers' "no new selection" rule. A
  fallback route can itself be deprecated or retired, so overloading `status`
  would lose that.
- **`enabled: false` refuses for everyone.** That is how an admin takes a model
  out of service (`grants_model_access` rule 1), and the fallback must stay in
  service.

So a separate boolean. It is excluded **server-side** from `GET /models`,
beside the existing `is_speech_model` filter in
`apis/app_api/models/routes.py`. Every picker (chat, Agent Designer, project
harness, voice) reads that list, so none of them needs a change and the SPA's
`pickableModels` stays as it is. The admin Manage Models list still shows the
row, with a "Fallback route for *{primary}*" badge.

### 2.2 Rules the admin write enforces

They live in one function, `fallback_pair_problems(primary, fallback) ->
list[str]`, in a new `apis/shared/models/model_fallback.py`. The admin create
and update routes call it, and the response names each problem.

1. The fallback row exists, is not the primary, is `enabled`, and is `active`.
2. **One hop.** The fallback has no `fallbackModelId` of its own. Chains turn a
   provider outage into a tour of the catalog, and every hop is another cache
   re-write.
3. **Same provider, Bedrock Converse only** in phase 1 (`ModelProvider.BEDROCK`
   for both). The OpenAI family's `us.openai.gpt-*` / `openai.gpt-*` pair is
   plausible for phase 2, but neither id appears in a Price List offer file, so
   its rates have to come from the cards.
4. **Same base model** (§6): equal after stripping the inference-profile prefix.
   There are three copies of that prefix strip today
   (`evals/judges.same_base_model`, `memory/tokens.py`,
   `bedrock_count_tokens.py`), each with a different prefix list. PR-1 moves one
   into `apis/shared/models/inference_profiles.py` and points the other three at
   it.
5. **No wider geography.** A `global.` primary may fall back to `us.`, but a
   `us.` primary may not fall back to `global.`. An admin who chose `us.` may have
   chosen it for data residency, and a silent switch to a profile that routes
   worldwide would undo that during the one window nobody is watching.
6. `fallback.maxInputTokens >= primary.maxInputTokens`. Compaction thresholds
   come from the primary's row (the turn's requested model), so a smaller
   fallback window could receive a history it cannot hold.
7. The fallback has its input, output and cache-write / cache-read prices set
   (non-null), so the call can never price to `None`.
8. The primary is not itself `fallbackOnly`, and a `fallbackOnly` row is not
   `isDefault`.

The runtime re-checks only what it depends on to build a correct request: rules
1, 2, 3 and 7, cheaply, from the row it already holds (§3.2). It does not
re-check 4–6, so the dev recipe (§10) can exercise the mechanism with a
cross-model pair. Product policy is the admin write's job, as with every other
catalog rule.

Deleting, disabling or retiring a fallback row does not need a cascade. The
runtime treats a missing, disabled or retired fallback as "no fallback" and
logs once. The admin page warns on the primary ("its fallback route is off").

## 3. Access: the fallback inherits the primary's grant

**Decision: inherit.** Making admins grant both ids was the alternative, and it
fails in the worst way. Every role edit would have to remember the pair, and a
forgotten grant would switch the fallback off silently, discovered only during
an outage. Inheriting also matches what the admin means: they granted "Claude
Opus 5.5", and the profile is routing.

### 3.1 It goes through the one predicate

`grants_model_access` gains one keyword, `via: Optional[ManagedModel] = None`,
meaning "the primary row this call falls back from". The rule, in order:

1. No row and the catalog was read → refused (unchanged).
2. Row `enabled: false` → refused (unchanged).
3. **Row `fallbackOnly` and `via is None` → refused, for everyone, `*` holders
   included.** A fallback route runs only as a fallback. This keeps it out of
   api-converse, the Agent Designer's binding validation, `set_default_model` and
   a hand-typed `model_id`, all of which already call this predicate.
4. **`via` given → refused unless `via.fallback_model_id == model_id`.
   Otherwise the decision is `grants_model_access(via.model_id, via, …)`**,
   judging the primary's grant against the primary's row.
5. Role grant or `*` → allowed (unchanged).
6. Legacy `availableToRoles` → allowed (unchanged).

No second check exists anywhere. `AppRoleService.can_access_model` and
`ModelAccessService` keep delegating, and `filter_accessible_models` now also
drops `fallbackOnly` rows through rule 3.

### 3.2 When it runs

In `_resolve_turn_model`, right after the primary passes. `resolve_effective_model`
already reads the catalog through `list_all_managed_models`, and it gains
`EffectiveModel.fallback: Optional[ManagedModel]`, resolved in the same pass
from the same list. No new read happens, and a warm config cache means no
network. Retirement runs first, so a retired primary's *successor's* fallback
applies and the retired row's own fallback is never consulted, the same as its
`enabled` flag. The access call is a pure function over sets the route already
holds, so the cost is microseconds. A refusal at this point means "no fallback
this turn": it is logged and the turn proceeds on the primary.

## 4. Where the switch happens

### 4.1 How a model call is retried today

Strands' event loop calls `model.stream(messages, tool_specs, system_prompt,
tool_choice=…, system_prompt_content=…, invocation_state=…, model_state=…,
cancel_signal=…, …)` (`strands/event_loop/streaming.py`, 1.63.0). On an
exception it fires `AfterModelCallEvent`. `TransientModelRetryStrategy` (our
subclass of `ModelRetryStrategy`) sleeps 2 s → 4 s → 8 s and sets
`event.retry`, up to `sdk_max_attempts = 4`. botocore is held to one attempt, so
the strategy is the only retry layer. Two rules already hold:

- `is_transient_bedrock_fault`: a modeled transient code (503, 500,
  `ModelNotReadyException`, `ModelTimeoutException`, throttles), a 5xx, or a
  connection failure / connect timeout. A **read timeout is excluded** because
  it is already the whole silence budget.
- `tag_partial_output` wraps every model's `stream` and marks an exception
  raised after a visible chunk (`contentBlockStart` / `contentBlockDelta`) so
  the strategy never retries it.

### 4.2 The fallback wrapper

`with_fallback(model, build_fallback, primary_id, fallback_id)`, in
`apis/shared/models/model_fallback.py`, patches the **instance's** `stream`, the
same pattern as `tag_partial_output`, `text_only_input` and
`converse_document_cap`. The primary keeps every other method (`count_tokens`,
`config`, `update_config`), so Strands, CountTokens and tracing see the same
object as today.

Factory order in `AgentFactory.create_agent`:

```
provider model  →  with_fallback  →  text_only_input / converse_document_cap  →  tag_partial_output
(innermost)                                                                      (outermost)
```

Each patch captures the `stream` beneath it. The projections run **outside**
the fallback, so the fallback receives the request already projected for this
call, and the request bytes sent to the two profiles are identical. The partial
output tag runs outermost, so a fallback that fails after visible output is
still never retried.

Per call:

```
breaker open for primary, or this (user, session) is sticky (§5.3)?
    └─ yes → stream from fallback
    └─ no  → stream from primary
              ├─ succeeds → done
              └─ raises before any visible chunk, and is a provider-side fault (§4.3)
                    → open breaker; stream the SAME args from fallback, now, no backoff
                         ├─ succeeds → done (ledger: served by fallback)
                         ├─ transient fault before output → raise the fallback's error;
                         │     the strategy backs off and the next attempt goes
                         │     straight to the fallback (breaker open)
                         └─ non-transient error (AccessDenied, ValidationException…)
                               → the fallback is misconfigured: disable it in this
                                 process for 10 min, log ERROR with both ids, and
                                 re-raise the PRIMARY's error so today's retries proceed
```

The last branch matters: **a broken fallback must never make a turn worse than
today.**

The fallback model is built **lazily, on the first fault**, from
`dataclasses.replace(primary_model_config, model_id=fallback_id)` through the
same `_create_bedrock_model`. A `CountTokensBedrockModel` builds a boto3 client,
which is tens of milliseconds, so the happy path never pays for it. Building
from the primary's `ModelConfig` instead of resolving the fallback row's own
params means the thinking dict, effort, `max_tokens`, cache config and
`additionalModelRequestFields` are identical by construction. That holds only
because the model is the same (§6). One instance serves every concurrent turn on
that agent, so the build is idempotent under a lock and the wrapper keeps no
per-turn state on itself. Per-turn facts go on the ledger (§4.4).

### 4.3 Which faults switch

**Switch:** `is_transient_bedrock_fault(e)` **and not** a throttle, raised before
any visible chunk. That covers 503, 500, `ModelNotReadyException`,
`ModelTimeoutException`, other 5xx, connection resets and connect timeouts. It
also covers an `EventStreamError` carrying one of those codes when nothing
visible was yielded: a mid-stream error whose only chunk was `messageStart` is
invisible to the user.

**Do not switch (phase 1):**

- **Throttles** (`ModelThrottledException`, `ThrottlingException`). They are
  usually short bursts that the 2 s backoff clears, and a switch would re-write
  the prefix (§5) for a blip. The fallback profile also has its own Service
  Quotas, sized separately (§8.2). If throttling on `global.` turns out to be
  quota exhaustion, falling back on the *last* attempt is a small phase 2 change
  with data behind it.
- **Read timeouts**, excluded for the reason `is_transient_bedrock_fault` gives.
- **Anything after visible output.** That rule is not negotiable.
- **4xx validation / access errors on the primary.** These are our bugs or
  config, not an outage, and the fallback would fail the same way.

### 4.4 Telling the coordinator which model served

The coordinator always passes `invocation_state` to `agent.stream_async`. Today
it passes one only when there is client data (`stream_coordinator.py`, the
`stream_async(prompt, invocation_state=…)` branch). It puts a `ServedModelLedger`
object under a reserved key, holding the turn's `(user_id, session_id)`, and
Strands hands that same object to `model.stream`. The wrapper sets
`ledger.current = (served_id, fallback_from | None)` immediately before yielding
a call's first chunk. At each `message_start` (one per model call), the
coordinator copies `ledger.current` into that call's `per_message_metadata`
entry. No index bookkeeping is needed, and an attempt that failed before output
produced neither a message nor a ledger update. Without a ledger (a path that
passes none), the wrapper still falls back but records nothing and keeps no
stickiness.

## 5. Prompt cache

### 5.1 The re-write, priced

Each inference profile is its own cache, so the first call on the fallback
writes the whole prefix (tools + system + history) at the cache-write premium,
1.25× the base input rate. The request bytes are identical (§4.2), so from the
second call on, the fallback reads its own cache normally. Nothing in the
prefix changes, and the prompt-cache contract holds.

Opus 5.5, Global primary ($0.20 cache read) → Geo fallback ($5.50 cache write):

| Prefix | Read on primary (would have been) | Write on fallback | Extra, once per conversation |
|---:|---:|---:|---:|
| 30k | $0.006 | $0.165 | **$0.159** |
| 60k | $0.012 | $0.330 | **$0.318** |
| 150k | $0.030 | $0.825 | **$0.795** |
| 400k | $0.080 | $2.200 | **$2.12** |

Then every turn on the fallback pays the Geo premium. For a typical Opus 5.5 turn
(60k read, 3k written, 1.5k out) that is $0.057 Global vs $0.063 Geo, **+$0.006
a turn**.

**Comparison: today's manual recovery is not free either.** "Try with another
model" from the outage branch switches the conversation to (say) Sonnet 5
Global, which re-writes the same prefix at $2.50/MTok: $0.15 at 60k. The
fallback costs about twice that re-write, but it keeps the model the user chose,
and it spares the user one or more failed attempts of 30–70 s each plus a manual
resend.

### 5.2 Should later turns stay on the fallback? Yes, priced

Going back to the primary while the fallback's cache is warm re-writes the
prefix a **second** time, on the primary, at $5.00/MTok: **$0.29 at 60k, $0.72
at 150k**. Staying on the warm fallback costs $0.02/MTok more on reads plus 10%
on new tokens, about $0.006 a turn. Break-even is about **50 turns at 60k**.
Conversations are much shorter than that, so flipping back while warm is always
the expensive choice.

Flipping back after an idle gap of at least the cache TTL is free, because
both caches are cold by then and either model would write the prefix anyway.

### 5.3 The breaker and the stickiness rule

Process-level state in `model_fallback.py`. No network, no DynamoDB, bounded.

- **Breaker, per primary id.** A provider-side fault on the primary opens it for
  **5 minutes from the most recent fault**. While open, calls go straight to the
  fallback, which saves the seconds a doomed primary attempt takes. When it
  closes, the next call that is not sticky tries the primary again, as a
  half-open probe whose only cost is latency if the primary is still down.
- **Stickiness, per `(user_id, session_id, primary_id)`.** A conversation that
  was served by the fallback stays on it while its last fallback call is less
  than **5 minutes** old (the message-level cache TTL), even after the breaker
  closes. The first call after a ≥5 min gap goes back to the primary for free
  (§5.2). The key is `(user, session)` and never the session alone (the
  CLAUDE.md conversation-owner rule). It is an LRU bounded at 1,024 entries, and
  it lives in the module, not on the agent: one session can be served by more
  than one agent, and an `@`-mention agent must see the same answer.
- With `AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL=1h`, the static prefix (tools +
  system, ~20–30k) keeps a 1h TTL on the primary, so a return after 5–60 minutes
  may read it there. The rule keys on the 5 min tail anyway: it is the bigger
  part of the prefix and the simpler rule.

On AgentCore Runtime V2 a microVM serves one `(user, session)` (affinity, see
`runtime_session_id_for`), so the "process-wide" breaker is in practice
per-conversation too. That is the right granularity, because the cache it
protects is per conversation. It also means one conversation learning about an
outage does not spare another the first failed attempt. That costs latency
only, never money.

### 5.4 Cost rows: `fallbackFrom` and `fallbackSwitched`

The metadata writer (`apis/shared/sessions/metadata.py`) stamps `modelSwitched`
when `modelInfo.modelId` differs from the previous call's. With the served id on
the row (§7), the first fallback call and the first call back on the primary
would both read as a user's model switch, and the classifier, which keys on the
toolConfig + system-prompt fingerprints that match here, would book them
`miss_avoidable`.

So the rule becomes: compare the **requested** ids (`fallbackFrom ?? modelId`).
If those match but the served ids differ, stamp **`fallbackSwitched: true`**
instead of `modelSwitched`. The spend stays in `wastedUsd`, and the flag lets
readers subtract it, the same discipline as `agentSwitched` / `modelSwitched` /
`rewound`. Consumers that split those today get a third bucket:

- `apis/app_api/admin/costs/service.py`: `fallbackSwitchMissCount` /
  `fallbackSwitchUsd`, disjoint from the agent and model pairs. The routes
  docstring's "check `modelSwitched` too" note gains it.
- `apis/shared/observability/emf.py`: `FallbackSwitchMiss`, plus a
  content-free `ModelFallbackServed` count with `ModelId` = the primary, so the
  PromptCache dashboard shows a fallback in use. The existing account-wide
  `bedrock-invocation-server-errors` alarm still fires on the primary's failed
  attempts. Operators still get paged; only users are spared.
- `apis/shared/sessions/ledger_rollups.py`, `apis/shared/evals/calls.py`
  (`"fallback switched"` as an explanation), and the
  `observability/content_policy.py` allowlists.

## 6. Phase 1 allows only the same model under another profile

**Decision: yes, same base model only.** Every reason points one way:

- **Request parameters.** §4.2 builds the fallback from the primary's
  `ModelConfig`. For the same model, the thinking dict (`_apply_thinking_binding`:
  summarized display + `drop_block`), the effort enum, `max_tokens` bounds and
  cache-point support are identical. For a different model they are not:
  `curated-models.ts` records that Opus 5.5 400s on `temperature`, `top_p` and
  `thinking.type.disabled`, and that other models accept those. Resolving a second
  param set on the failure path is exactly the code nobody runs until an outage.
- **Replayed reasoning.** Signed `reasoningContent` blocks are replayed every
  turn. The PR 2001 measurements show billing behaviour differs **per model**
  (Opus 5.5 / Haiku 5.5 bill replay while the prefix ahead is byte-identical;
  Sonnet 4.6 / Opus 4.7 always bill; Haiku 4.5 never does), and
  `compaction_policy.reasoning_billed` keys on the model. The same model under
  another profile is the same weights. `global.` already routes one request
  across Regions, so a signature is not tied to a Region, and replay behaves the
  same on both. For another model, Strands drops reasoning blocks only for
  non-Claude targets (`_rejects_reasoning_block`, Strands #5166), so a
  Claude-to-Claude cross-model replay is the case nobody has measured.
- **Context window.** The same model has the same window, so compaction
  thresholds stay true. Rule 6 is a belt for a hand-edited row.
- **Quality.** A cheaper model quietly answering is a product decision, not
  routing. When the model itself is down, the outage branch already offers the
  user "Try with *another model*", and the user makes that choice.

Cross-model fallback is phase 2 at the earliest, and only with a measured replay
test per pair.

## 7. Metering

Today the coordinator prices every call of a turn with
`main_agent_wrapper.model_config.model_id` (`stream_coordinator.py`, the
per-message cost loop) and builds the stored `ModelInfo` /
`pricingSnapshot` from `agent.model_config.model_id`. Both would name the
primary for a call the fallback served, priced at Global rates for a Geo call.

The change, all after the stream and never before the first token:

- Each `per_message_metadata` entry carries `served_model_id` and `fallback_from`
  from the ledger (§4.4).
- `_calculate_streaming_cost(model_id=…)`, `_get_pricing_snapshot`,
  `get_model_by_model_id` for the context window, and `ModelInfo.model_id` use
  the **served** id for that call. A turn whose call 1 ran on the primary and call
  2 on the fallback gets two rows, each priced from its own row, which already
  matches "one C# record per assistant message".
- The row gains `fallbackFrom: <primary id>` (`ModelInfo.fallbackFrom` and the
  extra field), only on a call the fallback served.
- The interrupted-turn estimator (`streaming/interrupted_usage.py`) takes the
  current ledger entry's served id.
- **`lastModel` stays the requested id.** `update_session_activity` reads
  `agent.model_config.model_id`, which is still the primary. It must stay so,
  because the SPA restores the picker from `lastModel` and a fallback-only id is
  not in `/models`.
- Quota: the user is charged what the call cost, Geo rates included. That is
  about 10% more on the turns an outage pushed onto the fallback, and it is the
  truthful number. Absorbing the premium centrally would need a second price,
  which the cost row has no place for.

The SPA's prompt-cache guard (`prompt-cache-guard.service.ts`) reads each
reply's `modelInfo.modelId` to decide whether a model switch would re-write the
cache. Seeing the fallback id, it would warn on an ordinary next send. It must
read `modelInfo.fallbackFrom ?? modelInfo.modelId` (PR-4).

## 8. TTFT, flag and quotas

### 8.1 TTFT: nothing on the happy path

| Where | Happy-path cost |
|---|---|
| `resolve_effective_model` picks the fallback row | none: same list, same pass |
| `grants_model_access(fallback, via=primary)` | a pure set check, microseconds |
| Agent build | `with_fallback` patches a method; the fallback model is **not** built |
| Agent cache key | gains `fallbackModelId` (changing it rebuilds once; the prompt bytes are unchanged, so no cache cost) |
| Each `model.stream` call | one dict lookup (breaker + sticky key) and one more generator layer |

PR-2 measures the per-call overhead at realistic scale (a dozens-of-tools agent
and a production-sized prompt; the TTFT recipe in `turn-path-ttft.md` §6) and
states the number. The expectation is well under 0.1 ms. On the failure path
the turn pays one failed primary attempt plus one lazy model build (~tens of
ms), against today's 30–70 s to an error.

### 8.2 Flag

Per the CLAUDE.md rules, this costs money (Geo profiles price about 10% above
Global, plus the one-off re-write) and changes behaviour on a provider fault, so
it gets an **in-development** flag:

- **Backend:** `MODEL_FALLBACK_ENABLED` / `CDK_MODEL_FALLBACK_ENABLED`, on
  app-api (the admin write accepts the two fields only while on, and returns 400
  otherwise, so inert config never accumulates) and on the AgentCore Runtime
  (written **only while on**, 28 bytes of the V2 environment budget). Only
  `"true"` enables, and an unset GitHub variable arrives as `""`.
- **SPA: yes, `features.modelFallback`.** It is admin-only: the Manage Models
  fields, the Add fallback route action, and the fallback caption (§9). Without
  it, a deployment with the backend off would offer admins a setting that does
  nothing.
- **At promotion it becomes a rollout switch, not a feature switch.** The
  per-row `fallbackModelId` is the deployment's real choice, because nothing
  happens and nothing is spent until an admin sets one. The env flag then only
  guards the wrapper, and it retires on the default condition (#1422).

### 8.3 Quotas and accounts

- **Each inference profile has its own Service Quotas** (tokens and requests
  per minute). Prod's `us.anthropic.claude-opus-5-5` quota has never carried
  prod's Opus traffic and may be at its default. **Prerequisite before turning
  this on in prod:** read the primary's peak tokens-per-minute from CloudWatch
  (`AWS/Bedrock`, `ModelId=global.anthropic.claude-opus-5-5`) and check the
  `us.` profile's quota against it, through the AWS Limits page
  (`/admin/aws-limits`, on in dev) or Service Quotas directly. A fallback that
  throttles on its first busy minute buys nothing. This is also why throttles
  are not a switch trigger in phase 1 (§4.3).
- **IAM needs no change.** The Runtime role's Bedrock grant already covers every
  inference-profile and foundation-model ARN (`inference-api-iam-roles.ts`).
- **Dev cannot test the motivating pair.** The dev account's SCP denies every
  `global.*` profile, so a dev primary is `us.*`. Rule 5 forbids `us.` →
  `global.` anyway, so there is no same-model pair on dev at all. §10 covers how
  to test it there.

## 9. Surfacing

The answer is the same model, so the user learns of it quietly, not as a
warning.

- **New SSE event `model_fallback`**, payload `{type, sessionId,
  requestedModelId, requestedModelName, servedModelId, reason}`, with `reason`
  one of `provider_unavailable` (a fault this turn) or `breaker_open` (skipped a
  known-down primary). It is emitted at most once per turn, drained by the
  coordinator like `agent_status` before the `message_start` of the first call
  the fallback served. It never reaches the prompt.
- **The SPA** shows one muted line under that answer: "*Claude Opus 5.5's usual
  route was unavailable, so this answer used a backup route for the same model.*"
  On reload the line is rebuilt from the message's `modelInfo.fallbackFrom`,
  which is already replayed with the message metadata, so there is no new row
  and no new read.
- **Both routes failed:** the outage branch's `provider_unavailable`
  `stream_error` still names the **requested** model (the coordinator reads
  `main_agent_wrapper.model_config.model_id`, still the primary) and its
  Try again / Try with another model buttons apply. `metadata` adds
  `fallback_model_id` and the copy says the backup route failed too. If that
  branch has not merged when PR-4 lands, PR-4 adds only the event and the
  caption, and the both-failed copy follows the branch.
- **The terminal client** logs the unknown event once per turn and is otherwise
  unaffected. The CLAUDE.md SSE table gains the row.

## 10. Dev test recipe

On dev only same-profile ids exist (§8.3), so the recipe proves the
**mechanism** with a cross-model pair and a forced fault. The same-model
guarantee is proved hermetically.

**Fault injection.** `MODEL_FALLBACK_FAULT_INJECT_MODEL_ID`, written to the
Runtime only from `CDK_MODEL_FALLBACK_FAULT_INJECT_MODEL_ID`, only while the
flag is on, and **refused at synth when `config.production` is true**. When the
primary's id matches, the wrapper raises a synthetic `ClientError`
(`ServiceUnavailableException`, HTTP 503) before sending the request, so no
tokens are billed for it.

Steps:

1. Set `CDK_MODEL_FALLBACK_ENABLED=true` and
   `CDK_MODEL_FALLBACK_FAULT_INJECT_MODEL_ID=us.anthropic.claude-sonnet-5-5` in
   the `development` GitHub environment, then deploy `platform.yml`.
2. Point dev's `us.anthropic.claude-sonnet-5-5` row at
   `us.anthropic.claude-opus-5-5` with a direct `update-item` on the
   managed-models table (`fallbackModelId`). Do **not** set `fallbackOnly` on the
   Opus row, since dev uses it. The admin form would refuse this pair under rule
   4, which is the point: the runtime does not check rule 4. Open a **new**
   session after deploy (memory note: dev validation needs a post-deploy
   session).
3. In a **fresh** conversation (no replayed reasoning in history), send on
   Sonnet 5.5. Expect: no `model_retry` frame, a `model_fallback` frame with
   `reason: provider_unavailable`, the answer streamed, and the caption shown.
4. `GET /admin/costs/sessions/{id}/calls`. Expect the call's `modelInfo.modelId`
   = Opus 5.5, `fallbackFrom` = Sonnet 5.5, priced at the Opus row's rates, and
   `fallbackSwitched` absent on this first call (no predecessor).
5. Send again within 5 min. Expect `reason: breaker_open`, a cache **read** on
   the Opus row (`cacheStatus: hit`), and no primary attempt in the Runtime log.
6. Unset the injection, wait past 5 min, and send. Expect the primary serves,
   the row has no `fallbackFrom`, it carries `fallbackSwitched: true`, and the
   cost view puts its re-write in the fallback bucket, not the model-switch one.
7. Reload the conversation. Expect the caption on the fallback answers only.
   Make a plain send and expect no prompt-cache-guard dialog.
8. Clear both dev variables and the row's `fallbackModelId` afterwards.

**Hermetic, in PR-2:** a golden-request case in `test_bedrock_golden_requests.py`
asserting that the fallback's formatted Converse request equals the primary's
byte for byte except `modelId`, for a `global.`/`us.` Opus 5.5 pair. That is the
claim §5.1 rests on. Plus the decision table in §4.2, one test per branch
(including "fallback misconfigured → the primary's error is re-raised and
retried"), and the breaker and stickiness keyed on `(user, session)`, including
two users on one session id.

**Prod (when turned on there):** the first real outage is the test.
`ModelFallbackServed` on the dashboard and `fallbackFrom` rows answer whether it
worked. Add a line to #1798 in the PR that turns it on.

## 11. Out of scope for phase 1

- **Other call paths.** Compaction's summarizer runs on the turn's model through
  its own client and will still fail during an outage. The compaction just does
  not happen that turn, as today. api-converse, voice and `document_extract` are
  phase 2: each needs the ledger and metering on its own path.
- Throttle-triggered fallback (§4.3), cross-model fallback (§6), the OpenAI
  family (§2.2 rule 3), and chains.
- **Shared state across microVMs.** A DynamoDB-backed breaker would spare each
  new conversation its one failed attempt. That costs a read on every turn, which
  the TTFT budget does not allow, to save seconds only during outages.

## 12. PR plan

Each PR keeps the flag off, so nothing changes in a deployment until every PR
has landed and an admin configures a pair.

| PR | Scope | Proof |
|---|---|---|
| **PR-1: data + access** | `fallbackModelId` / `fallbackOnly` on the three model shapes and the SPA type; `fallback_pair_problems`; one shared `inference_profiles.py` prefix strip (and the three copies pointed at it); `grants_model_access(…, via=)` rule 3/4; `/models` drops `fallbackOnly`; `EffectiveModel.fallback`; `MODEL_FALLBACK_ENABLED` reader + CDK config + `platform.yml` variable + CDK test (Runtime env written only while on). Inert at runtime. | Predicate table tests incl. `*` holder refused on a `fallbackOnly` row; architecture boundary test unchanged |
| **PR-2: runtime switch** | `with_fallback`, breaker, stickiness, `ServedModelLedger`, factory wiring and order, agent cache key, coordinator always passing `invocation_state`, fault injection + synth guard | Golden-request byte equality; decision-table tests; overhead measured and stated (§8.1) |
| **PR-3: metering + cost views** | served id per call for price, snapshot, window and `ModelInfo`; `fallbackFrom`; `fallbackSwitched` in the metadata writer; costs service, EMF, ledger rollups, evals explanations, content-policy allowlists | A mixed primary/fallback turn writes two rows priced from two rows; a switch-back books `fallbackSwitched`, never `modelSwitched` |
| **PR-4: SPA + SSE** | `model_fallback` event and caption; prompt-cache guard reads `fallbackFrom ?? modelId`; Manage Models fields, badge and Add fallback route (curated Regional rates) behind `features.modelFallback`; CLAUDE.md SSE table; outage-branch metadata if merged | Specs with `FEATURES` provided; axe on the form; checked in the in-app browser |
| **PR-5: dev validation + docs** | §10 on dev; docs-site feature-flags row (kind: rollout, retirement condition); CHANGELOG | The recipe's eight checks recorded in the PR |

PR-3 must land before any environment turns the flag on. Without it, a fallback
call is metered at the primary's Global rates, which is wrong but not unmetered.
