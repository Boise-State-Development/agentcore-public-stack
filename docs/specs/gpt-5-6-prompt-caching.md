# Plan: prompt caching for OpenAI GPT-5.6 on Bedrock

**Status:** Shipped and VERIFIED LIVE 2026-09-05 — PR-1 (#945), PR-2 (#949), PR-5 (#951), PR-4 (#954, shipped OFF via #956) and the IAM fix (#959). Caching confirmed working end-to-end through the agent loop: warm turns cost 10.6x less than cold. PR-3 (catalog rates) remains BLOCKED, but the blocker changed on 2026-09-06: these models bill through AWS Marketplace, which no pricing API covers, so no amount of waiting will publish them. The empirical route is open instead — Cost Explorer carries the dollars but names no model, so rates are only attributable on a single-model day. First controlled window claimed 2026-09-06 (Sol). Dollar figures stay provisional until it is read.
**Author:** (drafted with Claude)
**Date:** 2026-09-04
**Related:** `agents/main_agent/core/model_config.py`, `agents/main_agent/core/agent_factory.py`,
`apis/shared/models/mantle.py`, `apis/shared/bedrock/bearer_token.py`,
`apis/shared/costs/calculator.py`, `apis/shared/observability/prompt_cache.py`,
`frontend/.../admin/manage-models/models/curated-models.ts`,
`infrastructure/lib/constructs/inference-api/inference-api-iam-roles.ts`

## Summary

GPT-5.6 (Sol / Terra / Luna) supports both implicit and explicit prompt caching on
Bedrock, with a 1.25× cache-write premium and a 90%-off read — the same economics
shape as our Claude models, and therefore subject to the same prompt-cache contract
in `CLAUDE.md`.

None of our existing caching machinery reaches it. `CacheConfig(strategy="auto")`
and `cache_tools` are `BedrockModel` (Converse) features gated on Anthropic model
ids; GPT-5 is built as an `OpenAIResponsesModel`. **The auto cache config does not
apply and cannot be made to apply.**

The decision this doc makes: **route GPT-5.6 over the Responses API on the
`bedrock-runtime` endpoint**, fix the provider-shaped accounting bugs that path
exposes, and only then add explicit cache breakpoints.

## Background: caching is Responses-API-only

Two generations behave differently, and the boundary is at 5.6:

| | GPT-5.5 and earlier (incl. our curated `openai.gpt-5.4`) | GPT-5.6 Sol / Terra / Luna |
|---|---|---|
| Caching | Implicit only, automatic, no params | Implicit by default **+ explicit breakpoints** |
| Min prefix | 1,024 tokens | 1,024 tokens per breakpoint (max 4) |
| TTL | — | 30 min (`prompt_cache_options.ttl`, default `30m`) |
| Cache write fee | **None** — reads only | **1.25×** the input rate |
| Controls | none | `prompt_cache_breakpoint: {mode:"explicit"}` on content blocks, `prompt_cache_options.mode`, `prompt_cache_key` |
| Usage fields | `input_tokens_details.cached_tokens` | `cached_tokens` **and** `cache_write_tokens` |

And the endpoint/API matrix for 5.6, per its model card:

| | `bedrock-runtime` | `bedrock-mantle` |
|---|---|---|
| APIs | Responses ✅ · Chat Completions ✅ · **Converse ✅** · Invoke ❌ | Responses ✅ · Chat Completions ✅ · Converse ❌ |
| Prompt caching | ✅ **Responses API only** | ✅ **Responses API only** |
| Also gets | Guardrails (Converse only), application inference profiles (Converse only), invocation logs, CloudWatch, Cost Explorer itemization, Geo/Global CRIS | Server-side tool use, Projects, In-Region inference |
| Model id | `us.openai.gpt-5.6-sol` / `global.openai.gpt-5.6-sol` — **in-Region not offered here** | `openai.gpt-5.6-sol` |
| CountTokens | ❌ not supported | ❌ not supported |

The trap is that Converse is the *tempting* path — it drops straight into our
existing `BedrockModel` plumbing with SigV4 and no new auth — and it is the only
path with **zero** prompt caching. At Sol's published in-Region rates ($4.40/MTok
input, $0.44 cache read), our ~30k-token stable prefix costs ~$0.13/turn on
Converse against ~$0.013 on a Responses cache hit. Over a session that dwarfs the
Converse conveniences.

## Decision

**Responses API on `bedrock-runtime`.** Rationale:

1. Caching at all — the only reason this doc exists. Rules out Converse.
2. Versus Responses-on-Mantle: `bedrock-runtime` adds CRIS, invocation logging,
   CloudWatch metrics, and Cost Explorer itemization, and Global CRIS is cheaper
   ($4.00 vs $4.40 input for Sol short-context). We give up server-side tool use
   (we don't use it) and In-Region inference (not available on that endpoint for
   this model anyway).
3. It leaves the Mantle path untouched for `openai.gpt-5.4` and Gemma/Qwen, so
   nothing already shipped moves.

⚠️ **Pricing above is transcribed from the model card and must be re-verified
against the Price List API before any catalog row ships.** Note the direction is
*inverted* from our Claude rule of thumb: for GPT-5.6 the `global.*` profile is
the cheaper one, not the more expensive. Don't inherit the Claude assumption.

## What Strands 1.51.0 gives us

Gives us one thing: `input_tokens_details.cached_tokens` → `cacheReadInputTokens`
(`models/openai_responses.py:894`). Also useful: `config["params"]` is spread
verbatim into `responses.create()` (`:562`), so top-level request params pass
through with no SDK change.

Does **not** give us (verified by grepping the installed package — zero hits):
`prompt_cache_breakpoint`, `prompt_cache_options`, `prompt_cache_key`,
`cache_write_tokens`. And `bedrock_mantle_config` hardcodes the Mantle host
(`models/_openai_bedrock.py:19`) while *rejecting* a caller-supplied `base_url` /
`api_key` when it is set (`models/openai_responses.py:184`) — so pointing at
`bedrock-runtime` means not using that config at all.

## Work plan

### PR-1 — Normalize OpenAI usage semantics (correctness; blocks the rest) ✅ SHIPPED (#945)

The bug that bites the moment any GPT-5 turn runs, caching or not:

OpenAI's `input_tokens` **includes** cached tokens. Bedrock Converse's
`inputTokens` **excludes** them. Strands passes `input_tokens` straight through as
`inputTokens` and reports `cacheReadInputTokens` alongside it. Our
`CostCalculator` documents and relies on the buckets being disjoint
(`apis/shared/costs/calculator.py:71`) and sums all three — so every cached token
is billed at the full input rate *and* the cache-read rate. The same assumption
is baked into the context-attribution sum at `stream_coordinator.py:725`.

- Normalize on the OpenAI-family path before usage reaches the calculator:
  `inputTokens -= (cacheReadInputTokens + cacheWriteInputTokens)`, clamped at 0.
- Map `cache_write_tokens` → `cacheWriteInputTokens` (Strands drops it; this is
  the one field that makes 5.6's 1.25× premium visible at all). Upstream a patch
  to `strands-agents` in parallel — we shouldn't carry this forever.
- Do it once, in a provider-aware shim, not at each of the several call sites that
  read the usage dict.

**Tests:** a usage-mapping unit test asserting the disjoint invariant per provider,
and a calculator test proving a fully-cached GPT-5.6 call costs
`cached × readRate`, not `cached × (inputRate + readRate)`.

#### ⚠️ Correction: subtract the write bucket too

The first draft of this section said `inputTokens -= cacheReadInputTokens`. That
was written when `cacheWriteInputTokens` was always 0 — Strands drops the field —
so only reads could double-count. The moment the bullet above maps
`cache_write_tokens`, the *same* double-count reappears for writes, and worse: at
`inputRate + 1.25×inputRate` rather than `inputRate + 0.1×inputRate`.

AWS's GPT-5.6 prompt-caching guidance states the identity outright:

```
input_tokens = cached_tokens + cache_write_tokens + non-cached remainder
```

Both cache buckets are *inside* the inclusive total, so restoring disjointness
means subtracting both. #945 ships it that way; the bullet above is corrected to
match.

#### What actually shipped

`apis/shared/models/usage_normalization.py`:

- `normalize_usage(usage, provider)` — Bedrock passes through untouched; OpenAI
  gets both cache buckets subtracted out of `inputTokens`, clamped at 0.
- `openai_cache_write_tokens(usage_obj)` — reads
  `input_tokens_details.cache_write_tokens` (also checks the top level, where some
  OpenAI-compatible gateways hoist it).
- `usage_normalized(model_cls)` — memoized subclass applying both while the model
  formats its `metadata` chunk.

The seam is the **model class**, not any downstream usage reader: Strands destroys
`cache_write_tokens` inside its chunk formatter, so by the time usage reaches
`stream_processor._extract_usage_data` the field is unrecoverable. Installed at the
two OpenAI-family *construction* sites — `build_mantle_model` and
`AgentFactory._create_openai_model`. The two SDK classes disagree on the method
name (`OpenAIResponsesModel._format_chunk` is private,
`OpenAIModel.format_chunk` is public), which the shim resolves and pins with a
contract test.

⚠️ **The convention follows the model family, not the adapter.** GPT-5.6 routed
over *Converse* on `bedrock-runtime` reports **disjoint** buckets — the Bedrock
convention — measured by a third party on
[strands-agents/harness-sdk#3546](https://github.com/strands-agents/harness-sdk/issues/3546).
Keying the shim off the OpenAI model class is correct for the Responses transport
PR-2 builds, but a Converse-routed OpenAI model must **not** be wrapped or its
input will be under-counted. Anything that adds an OpenAI model on the Converse
path has to opt out.

**Upstream:** [harness-sdk#4193](https://github.com/strands-agents/harness-sdk/pull/4193)
maps the dropped `cache_write_tokens`. Note the prior art before proposing anything
broader: #3546 is an open umbrella bug for this exact convention split, and the
84-file fix for it (#3561) was closed unmerged — maintainers want small,
independently-reviewable PRs. A maintainer on that thread also notes AgentCore
GenAI Observability currently double-counts these tokens in its own cost display
and suggests pinning `strands-agents==1.53.0`; we are on 1.51.0.

### PR-2 — `bedrock-runtime` Responses transport

- New transport target alongside Mantle. Do **not** pass `bedrock_mantle_config`;
  pass plain `client_args` with
  `base_url="https://bedrock-runtime.{region}.amazonaws.com/openai/v1"` and an
  `api_key` minted by `apis/shared/bedrock/bearer_token.py` (already produces
  exactly the right credential).
- **Token refresh:** Mantle config re-mints per request; a static `api_key` in
  `client_args` freezes at construction. Our microVMs live 18–50 min against a
  12-hour token cap so it would work by luck — instead override
  `_resolve_client_args()` (called per request) to mint fresh. A handful of lines,
  and it removes a class of "worked in dev, expired in prod" failure.
- **Model id:** requests must name `us.` or `global.` prefixed inference profiles.
  Reuse the existing region/profile plumbing rather than string-munging ids at the
  call site.
- **IAM:** `bedrock-runtime` OpenAI access additionally requires
  `bedrock:InvokeModel` on the account's **default project ARN**
  (`arn:aws:bedrock:{region}:{account}:project/default`) on top of the inference
  profile. Not present in `inference-api-iam-roles.ts` today — this is a
  `platform.yml` deploy, so sequence it ahead of the backend change.

#### ⚠️ CORRECTED TWICE — the real IAM gap was a different action

**The `InvokeModel` half of that bullet was already satisfied.** Simulated against
the deployed dev roles with `iam simulate-principal-policy`:

| action | resource | runtime role |
|---|---|---|
| `bedrock:InvokeModel` | `inference-profile/us.openai.gpt-5.6-sol` | allowed |
| `bedrock:InvokeModel` | `project/default` | allowed |
| `bedrock:InvokeModelWithResponseStream` | inference profile | allowed |
| **`bedrock:CallWithBearerToken`** | any | **implicitDeny** |

The account-scoped `arn:aws:bedrock:{region}:{account}:*` resource on the existing
`BedrockModelInvocation` statement already matches `project/default`, so PR-2
shipped without an IAM change on that basis — correctly, as far as it went.

**What PR-2 missed is the bearer token itself.** The `bedrock-runtime`
OpenAI-compatible endpoint authenticates with the same short-term token
construction as Mantle, but authorizes it under a *different IAM service
namespace*: `bedrock:CallWithBearerToken`, not
`bedrock-mantle:CallWithBearerToken`. Only the Mantle one was granted, so the
first real turn failed:

```
401 ... is not authorized to perform: bedrock:CallWithBearerToken on resource: *
because no identity-based policy allows the action
```

Both roles need it — the AgentCore runtime role (agent loop) and the app-api task
role (`/chat/api-converse`). **This IS a `platform.yml` deploy**, and it must land
before the transport can serve a single turn.

Why no earlier step caught it: unit tests construct the model but never issue a
request, and the `probe_gpt56_cache_rates.py` run that verified PR-1/2/5 executed
under a developer's SSO credentials, which carry far broader permissions than the
runtime role. Only an end-to-end turn through the deployed agent exercises the
actual principal. Worth remembering as a general shape — a transport that
authenticates differently from its neighbours needs its IAM verified against the
*deployed role*, not inferred from the neighbour's grants.

**Boundary check:** this is model-transport code, so it belongs in
`apis/shared/models/` next to `mantle.py`, consumed by both `agent_factory` and the
API-key `/chat/api-converse` handler. Don't fork the build logic.

### PR-3 — Catalog entry and pricing

- Add GPT-5.6 to `CURATED_MANTLE_MODELS`' sibling set (or a new
  `CURATED_RUNTIME_OPENAI_MODELS` if the transport field warrants a separate tab).
- `supportsCaching: true`, plus verified `cacheReadPricePerMillionTokens` and
  `cacheWritePricePerMillionTokens`. Note `mantleDefaults()` hardcodes
  `supportsCaching: false` (`curated-models.ts:233`) — GPT-5.6 must not inherit it.
- For `openai.gpt-5.4` / 5.5, if we keep them: `supportsCaching: true` with
  `cacheWritePricePerMillionTokens: 0`. That is correct (no write fee) and
  conveniently makes `compute_wasted_usd` see a non-positive premium and return
  $0 rather than inventing waste.
- Re-verify every rate against the Price List API, per the ⚠️ above.

#### ⛔ BLOCKED — the Price List API does not publish these rates

Checked 2026-09-05 against dev-ai (490617140655) with SSO credentials, across
every Bedrock service code:

| Service code | GPT-5.6 coverage |
|---|---|
| `AmazonBedrock` | `openai.gpt-5.6-terra` + `-luna` only, **`us-gov-west-1` only**, and only `-mantle-` usage types. **`sol` absent entirely.** |
| `AmazonBedrockService` | `provider` attribute values are `Anthropic` and `Luma AI` only — no OpenAI |
| `AmazonBedrockFoundationModels` | no GPT entries in `servicename` |
| `AmazonBedrockAgentCore` | AgentCore consumption SKUs, not foundation-model pricing |

**Commercial-region rates for the GPT-5.6 family are not in the Price List API
at all.** Neither are commercial `openai.gpt-5.4` rates — so the shipped
`$2.75 / $16.50` row on that model was never Price-List-verified either; it
came from the model card. This PR's own gate therefore cannot be satisfied
today, and PR-3 is deferred rather than shipped on transcribed numbers.

Options when it is picked back up, in preference order:

1. Wait for AWS to publish commercial rates and verify as specced.
2. Derive them empirically — add the model through the admin escape-hatch
   form (the PR-2 transport already supports it), drive real turns via the
   dev-ai experiment harness, and reconcile against Cost Explorer to back out
   per-token rates. Stronger evidence than a published table, but Cost
   Explorer lags ~24h.
3. Ship model-card rates explicitly labelled unverified, in code and in the
   PR. Last resort: these rows price real spend against faculty quotas.


#### 2026-09-06 — Option 2 attempted: what Cost Explorer can and cannot say

Option 2 was run against dev-ai. It is viable, but only under a constraint the
plan above did not anticipate, and it closed off Option 1 in the process.

**Option 1 is not a waiting game.** These models bill through **AWS
Marketplace**, and the Price List API has no Marketplace service code — all 269
service codes were enumerated on 2026-09-06 and none covers Marketplace. The
Marketplace Catalog API is seller-side and returns nothing for a subscriber.
So the earlier "not published yet" reading was wrong: there is no pricing API
that *could* carry these rates while they bill this way. Waiting will not
produce them.

**Cost Explorer has the dollars, but names no model.** Usage types look like
`USW2-MP:USW2_cache_read_tokens_standard-Units` — they carry the token bucket
and the service tier, never the model id. Every OpenAI-family model in the
account shares the same four rows. There is no finer dimension: checked
`USAGE_TYPE` grouped by `OPERATION` (all `InvokeModelStreamingInference`) and
by `BILLING_ENTITY` (all `AWS Marketplace`).

**Therefore a rate is attributable only on a single-model day.** That is the
method, and it works — dev has near-zero organic OpenAI traffic, so clean days
are easy to claim. `probe_gpt56_cache_rates.py --rates-only --table <sessions
table>` now prints which models we recorded that day and refuses to vouch for a
number when more than one OpenAI-family model ran.

Two traps, both hit and both now guarded in the script:

- **Read it DAILY, not monthly.** Daily rows come back as exact round numbers;
  a multi-day window silently blends models into a meaningless average. August
  shows two distinct price cards — `$5.50 / $27.50` and `$2.20 / $11.00` — and
  2026-08-31 is visibly a blend of the two (`$4.3780` input). A monthly read
  would have reported that blend as if it were a rate.
- **The unit is `1M tokens`, not `1K`.** Cost Explorer declares it in the
  `Unit` field, and it differs by billing path: Marketplace rows are `1M
  tokens`, natively-billed rows (Nova, Titan, Mantle-served models) are `1K
  tokens`. The script previously assumed 1K for everything, which overstated
  every Marketplace rate by 1000x. It now reads the declared unit.

**The cache ratios hold, independently confirmed.** On every clean day, in both
price cards, cache read is exactly `0.1x` input and cache write exactly
`1.25x`. This is commercial-region billing data, and it corroborates the
GovCloud ratio finding below from a completely different source.

**Bearing on the modelling gap.** Every row observed is `_standard`; no
`-long-ctx` usage type has ever appeared in this account. So the 0.5x/2x tier
and 2x long-context dimensions are not currently being billed against us, and a
flat standard rate is correct *for our present traffic*. That downgrades the
gap from "silently mis-prices" to "mis-prices only if traffic changes, and Cost
Explorer will show a new usage type when it does" — which is a monitorable
condition rather than a blocking unknown.

**Controlled window claimed: 2026-09-06, `us.openai.gpt-5.6-sol` only.** Dev
had zero recorded model calls that day before the probe. Expected totals, to be
divided into that day's Cost Explorer dollars:

| bucket | tokens |
|---|---|
| `inputTokens` | 2,660 |
| `cacheReadInputTokens` | 17,496 |
| `cacheWriteInputTokens` | 5,868 |
| `outputTokens` | 50 |

Two arms produced these: an 8,000-token prefix over 4 turns for the cache
buckets, and a 600-token prefix over 6 turns — deliberately under the
1024-token minimum cacheable prefix, so nothing caches and input tokens
accumulate as the rate anchor. Read it once Marketplace settles (allow 24-48h,
not 24h) and Terra and Luna need their own single-model days.

**What the read will actually settle.** The dev rows currently carry:

| model | input | output | cache read | cache write |
|---|---:|---:|---:|---:|
| `us.openai.gpt-5.6-sol` | 4.40 | 26.40 | 0.44 | 5.50 |
| `us.openai.gpt-5.6-terra` | 2.64 | 15.84 | 0.264 | 3.30 |
| `us.openai.gpt-5.6-luna` | 0.264 | 1.584 | 0.0264 | 0.33 |

The cache columns are not the risk — they are `0.1x` and `1.25x` of input, now
confirmed from two independent sources. The risk is concentrated in two places:

- **Every output rate is a guess.** They are `6x` input, a ratio taken from the
  GovCloud Terra row. Commercial daily billing shows both price cards running
  at `1:5`, not `1:6` — so if GPT-5.6 also prices at `1:5`, Sol's output rate is
  overstated by 20%. Output is the largest per-token number in each row.
- **Sol's input rate has no source at all.** Terra and Luna at least descend
  from GovCloud Price List rows; `sol` is absent from the Price List entirely,
  so `4.40` came from the model card.

The single-model day yields input and output directly, which settles both.

What the GovCloud rows *do* establish, and can be relied on:

- The **ratios are exact**. Terra standard: input `2.64`, cache read `0.264`
  (0.1x), cache write `3.30` (1.25x). The cache economics this whole plan
  rests on are confirmed.
- The **30-minute TTL is confirmed** by the SKU naming itself — the write
  usage types are `...-cache-write-tokens-30m-...`. That is the corroboration
  PR-5 needed.

#### ⚠️ Modeling gap PR-3 must resolve before it ships

The Price List rows carry two pricing dimensions `CuratedModel` cannot
represent:

- **Service tiers.** Every model is priced at `flex` / `standard` / `priority`
  = 0.5x / 1x / 2x.
- **Long context.** Every rate has a `-long-ctx` twin at **2x** the base.

Our catalog holds one flat rate per bucket, so a GPT-5.6 row would mis-price
any long-context turn by 2x — silently, as a plausible-looking number rather
than an error, which is exactly the failure the Verification section exists to
catch. Decide before curating: either model the dimensions, or scope the row
to standard-tier short-context and gate on staying inside it.

### PR-4 — Explicit cache breakpoints + `prompt_cache_key` ⛔ SHIPPED OFF (measured pessimization)

Only worth building for 5.6, where the 1.25× write premium makes placement matter.

- `prompt_cache_key` is free — it rides `config["params"]` through the existing
  spread. Key it off fingerprints we already compute:
  `f"{systemPromptHash}:{toolConfigHash}"`, so requests sharing a prefix route to
  the same cache and a config change rotates the key by construction.
- Explicit breakpoints need a `BedrockResponsesModel(OpenAIResponsesModel)`
  subclass overriding `format_request` to stamp
  `prompt_cache_breakpoint: {mode: "explicit"}` on the last stable content block
  (after tool definitions and the system/developer message, before conversation
  history), plus `prompt_cache_options: {mode: "explicit", ttl: "30m"}`.
- This buys us the Claude-path resilience we already depend on: a message-level
  miss costs a *read* of the ~30k static prefix instead of a full re-write.
- The existing prompt-cache contract carries over unchanged — implicit and
  explicit caching are both exact-prefix, so deterministic tool/skill ordering and
  the truncation anchor in `TurnBasedSessionManager` remain load-bearing.

#### Two corrections from building it

**`prompt_cache_key` does not ride `config["params"]`.** It is a first-class
parameter on the OpenAI SDK's `responses.create`, so it is set directly on the
formatted request. `prompt_cache_options` is *not* a named parameter and has to
travel in `extra_body`. Both are set in the `_format_request` override rather
than at model construction, because that is the only seam where the system
prompt and tool specs — the inputs the key is derived from — are actually in
scope.

**There is no "system/developer message" to stamp in Strands' output.** It emits
the system prompt as the top-level `instructions` *string*, and a breakpoint has
to sit on a content **block**. So the override re-expresses `instructions` as the
`developer` message AWS's guidance shows, at the head of `input`, carrying the
breakpoint:

```python
{"type": "message", "role": "developer",
 "content": [{"type": "input_text", "text": ...,
              "prompt_cache_breakpoint": {"mode": "explicit"}}]}
```

That places the boundary exactly at the end of the static prefix (tools +
system), before any history. Stamping `input[0]`'s existing block instead would
have put the first user message inside the cached segment, so compaction — which
rewrites history — would invalidate the tools+system segment too.

#### Risk this PR carries

Explicit mode **opts out of** the model's default implicit caching. A badly
placed boundary is therefore worse than not switching at all. Two mitigations:

- With no system prompt there is no static prefix to bound, so the request is
  left untouched and implicit caching stays on.
- Kill switch `BEDROCK_RESPONSES_EXPLICIT_CACHE_ENABLED` (default ON).
  ⚠️ Deliberately **not** wired into the CDK Runtime construct: that construct is
  at the `AWS::BedrockAgentCore::Runtime` 50-variable cap, and a 51st entry fails
  changeset validation after CI is green. Flipping it in a deployed environment
  needs an out-of-band Runtime update.

#### ⛔ Measured live — the premise was wrong, so this ships OFF

Run 2026-09-05 against `us.openai.gpt-5.6-sol` (dev-ai, us-west-2) via
`backend/scripts/probe_gpt56_cache_rates.py --mode both --grow-history`:
8k-token static prefix, 5 turns, ~1.5k tokens of history growth per turn.

|          | uncached input | cacheRead | cacheWrite | input-equivalents |
|----------|---------------:|----------:|-----------:|------------------:|
| explicit |     **22,790** |    23,228 |      5,807 |        **32,372** |
| implicit |         **10** |    38,410 |     13,405 |        **20,607** |

(Input-equivalents price the buckets at the Price List ratios this same
investigation confirmed: input 1×, cache read 0.1×, cache write 1.25×.)

**Explicit cost ~57% more.** Per turn, explicit's uncached input grew
1,516 → 7,600 while its `cacheRead` stayed flat at 5,807; implicit held
uncached input at 2/turn and let `cacheRead` grow with the conversation.

The reasoning above had the *counterfactual* wrong. Implicit caching does not
re-write history when it grows — it appends the delta (measured: 1,521
cacheWrite per turn). So a breakpoint after the static prefix saves no
re-write; it only stops the history being cached at all, and the cost of that
grows linearly with conversation length.

The code ships behind `BEDROCK_RESPONSES_EXPLICIT_CACHE_ENABLED`, **default
OFF**, so the production path is byte-identical to stock Strands. It is kept
rather than deleted because the *placement*, not the mechanism, is what failed
— the API allows up to 4 breakpoints, and a scheme that also marks the end of
history might beat implicit. **Do not re-enable without re-running that probe
and beating the implicit arm.**

Untested idea worth its own measurement: `prompt_cache_key` is currently tied
to the explicit path, so it is off too. Applying it on the implicit path is
plausibly free and helps cross-request routing, but that is a fleet-level
effect a single-session probe cannot show — measure before shipping it.

### PR-5 — Observability

- ✅ **DONE.** `CACHE_TTL_SECONDS = 300` was wrong by 6× for OpenAI's
  30-minute TTL, so `classify_cache_status` called `miss_ttl_expired` on
  entries that were still live. The TTL is now model-derived via
  `cache_ttl_seconds_for(provider, model_id)`, threaded through
  `classify_cache_status(ttl_seconds=...)` from the serving model's
  `ModelInfo`. Only `bedrock-responses` gets the 30-minute window —
  deliberately **not** the whole OpenAI family, since `mantle`'s
  implicit-only caching has no documented 30m retention and guessing there
  would over-report waste. `CACHE_TTL_SECONDS` stays as the default for
  existing importers.

  Note the error direction, which is why this mattered: a TTL shorter than
  the model's real one *hides* waste. `partial_miss` is gated on
  `gap <= ttl`, so it degraded to `hit`; `miss_avoidable` degraded to
  `miss_ttl_expired`. Both zero `wastedUsd` — the metric that exists to
  catch this exact class of bug.
- Now that PR-1 has landed `cacheWriteInputTokens`, `partial_miss` / `miss_avoidable` /
  `wastedUsd` start working for GPT-5.6 as they do for Claude. Until it lands,
  every call classifies as `hit` or `uncached` and `wastedUsd` is structurally
  $0 — a silent blind spot, which is exactly the failure mode that let the
  compaction spiral run unnoticed.

## Known non-blocking issues

- **`use_native_token_count`** is set unconditionally on the Bedrock path in
  `to_bedrock_config`, and CountTokens is unsupported for GPT-5.6. Strands
  degrades to the chars/4 heuristic and caches the skip, so it costs one failed
  call per model — but the code comment asserting "Every catalog model is Claude
  family and supports the API" stops being true, and context attribution quietly
  loses its authoritative counts. Fix the comment; consider gating the flag.
- **Auto-strategy warning noise.** If a GPT model is registered under the Bedrock
  provider with `caching_enabled`, Strands logs "does not support automatic
  caching" every turn. Harmless — `bedrock_cache_points_supported()`
  (`model_config.py:329`) already prevents the tools/system cachePoints that would
  otherwise be a `ValidationException` — but worth suppressing.
- **Structured outputs and server-side tool use** are unsupported for GPT-5.6 on
  `bedrock-runtime`. Neither is on the agent path today; confirm before any
  feature starts depending on them for this model.

## ✅ VERIFIED END-TO-END — 2026-09-05, dev, through the agent loop

Session `f76ab27a-51a6-4f70-be93-e94c81e01d85`, `us.openai.gpt-5.6-sol` selected in
the SPA model picker, four turns, read back from
`GET /admin/costs/sessions/{id}/calls`:

| turn | cacheStatus | input | cacheRead | cacheWrite | output | cost |
|---|---|---:|---:|---:|---:|---:|
| 1 | `first_write` | 2 | 0 | 3,679 | 5 | $0.02038 |
| 2 | `hit` | 2 | 3,679 | 21 | 6 | $0.00190 |
| 3 | `hit` | 2 | 3,700 | 22 | 6 | $0.00192 |
| 4 | `hit` | 2 | 3,722 | 22 | 6 | $0.00193 |

**Warm turns cost 10.6x less than the cold turn.** Every prediction this plan
made holds, and each PR is confirmed by a specific column:

- **PR-1 — usage normalization.** `inputTokens` is **2** on every turn, not
  ~3,681. OpenAI reports `input_tokens` inclusive of both cache buckets; the
  buckets here are disjoint and sum exactly to the prefix
  (2+0+3,679 = 3,681; 2+3,679+21 = 3,702 — matching the SPA's context-window
  readout to the token). `cacheWriteInputTokens` is **populated at all**, which
  is only possible because we recover `cache_write_tokens` — Strands drops it.
  Turn 1 alone would otherwise have double-billed 3,679 tokens at the input
  rate *plus* the 1.25x premium, which is precisely the correction #947 made to
  this spec.
- **PR-2 — transport.** Reached a live model: base URL, per-request bearer
  token, inference-profile id all correct.
- **PR-5 — model-derived TTL.** Gaps of 30s / 78s / 18s classified `hit`, and
  `wastedUsd = $0.00` on every row with no avoidable re-writes.
- **#956 — explicit caching OFF.** This is the implicit shape the probe
  predicted: `cacheRead` **grows with the conversation** (3,679 -> 3,700 ->
  3,722) while `cacheWrite` is just the appended delta (~21). Under explicit
  mode `cacheRead` would be flat and uncached input would climb every turn.
  The 57%-worse finding is confirmed in the agent loop, not just at the
  transport.
- **#959 — IAM.** `bedrock:CallWithBearerToken`, without which none of the
  above could run.

Prefix fingerprints were stable exactly where they should be: `toolConfigHash`
`8eafb0765ed810a2` and `systemPromptHash` `5a71749b3bee37ab` identical across
all four calls, `historyHash` changing each turn.

The cost math reproduces to the cent from the disjoint buckets and the
catalog rates, so `CostCalculator` is verified against live usage as well.

⚠️ The **dollar amounts are provisional** — they use the model-card rates
seeded on the dev row (4.40 in / 26.40 out / 0.44 cache read / 5.50 cache
write), and 26.40 is *derived* from the GovCloud 1:6 input:output ratio rather
than published. The **ratios** above are real; the absolute dollars wait on
PR-3.

## ⚠️ `global.*` inference profiles are blocked **in dev** by an organization SCP

Discovered 2026-09-05 while adding GPT-5.6 Luna. Adding it as
`global.openai.gpt-5.6-luna` fails at the first turn:

```
401 ... not authorized to perform: bedrock:InvokeModel on resource:
arn:aws:bedrock:::foundation-model/openai.gpt-5.6-luna
with an explicit deny in a service control policy:
arn:aws:organizations::977099011063:policy/o-09d6ih8vwl/service_control_policy/p-r61tynkc
```

Editing that same row to `us.openai.gpt-5.6-luna` — same model, same account,
same region, **only the profile prefix changed** — succeeds. That isolates the
prefix as the cause: the deny is on the **region-less foundation-model ARN**
that a Global CRIS profile resolves to, not on the model.

### Scope: dev only — **prod is not affected**

Confirmed by Phil: `global.*` is **not** blocked in prod, which is the account
that matters for the cost model. The Decision section's preference for
`bedrock-runtime` on Global CRIS pricing ($4.00 vs $4.40 input for Sol
short-context) therefore **stands** — the ~9% discount is available where the
spend is.

What this actually costs us is a **dev/prod model-id divergence**: a GPT-5.6
row must be `us.*` in dev and can be `global.*` in prod. Anything that copies a
model row between environments — a seed script, a curated catalog entry, a
runbook — has to carry the prefix per environment rather than assume one id
works in both.

⚠️ **Method note for whoever hits this next.** `aws iam
simulate-principal-policy` does **not** evaluate SCPs, only identity policies.
Every simulation of the runtime role came back `allowed` while the real call
was denied. An SCP deny is only observable by actually invoking, which is also
why this surfaced at the first turn rather than in any earlier check.

Note the direction is the **opposite** of the Claude-family rule of thumb,
where `us.*` costs ~10% *more* than `global.*`.

## ⚠️ Mantle's `openai.gpt-5.4` caches, and was priced at $0.00

Measured live 2026-09-05, two turns:

| turn | status | input | cacheRead | cost |
|---|---|---:|---:|---:|
| 1 | `uncached` | 3,681 | 0 | $0.01021 |
| 2 | `hit` | 60 | **3,642** | $0.000264 |

Turn 2's cost reproduces exactly from input + output alone, so the 3,642 cached
tokens contributed nothing — a ~5.5x under-report on that turn, on a model in
daily use. The Price List confirms the model has a cache-read SKU ($0.33
GovCloud) and **no cache-write SKU**, exactly as PR-3 predicted.

Root cause was the admin form, not the data: the caching block was gated to
`bedrock` / `bedrock-responses`, so for a Mantle model the checkbox and the
cache-rate fields were never rendered and the row could only carry the
provider default of `false`. Fixed in #963 by adding
`CACHING_CAPABLE_PROVIDERS` (wider than the defaults list — Mantle stays off by
default, it just becomes selectable).

### The dashboard was wrong in BOTH directions

`Cost Analytics` computes `savings = cacheRead x (inputPrice - cacheReadPrice)`
per message from the pricing snapshot
(`app_api/sessions/services/metadata.py:257`). With `cacheReadPricePerMtok`
absent it reads as `0`, so the same missing rate produced two compounding
errors on `gpt-5.4`:

- **cost understated** — cached tokens priced at $0.00
- **savings overstated** — credited as a *100%* saving rather than 90%

So the model looked cheaper than it was *and* more efficient than it was, and
it carried ~5x the traffic of any GPT-5.6 row ($0.38 vs $0.07 in the window).
The GPT-5.6 rows were unaffected: they carry both rates, so their savings were
correct from the start.

### ✅ Fixed 2026-09-05 (dev)

Set through the admin UI once #963 deployed: `supportsCaching: true`,
`cacheReadPricePerMillionTokens: 0.275` (0.1x our catalog's $2.75 input,
matching the family ratio), `cacheWritePricePerMillionTokens: 0` (the Price
List has no cache-write SKU for this model).

Re-measured on an identical turn shape — 60 input, 3,642 cacheRead, 6 output:

| | cost |
|---|---:|
| before | $0.000264 |
| after | **$0.0012655** |

Reproduces exactly as `60x2.75 + 6x16.50 + 3,642x0.275` per MTok, so the
cached tokens now contribute $0.001002 instead of nothing — 4.8x more accurate
on that turn.

### ✅ PROD fixed 2026-09-05T17:41Z

Prod had exactly the same shape — `supportsCaching: False`, no cache-read rate,
on an **enabled** model that seven assistants hard-bind. Now set to
`supportsCaching: true` / `cacheRead 0.275` / `cacheWrite 0` and verified on
the record.

Done through `PUT /api/admin/managed-models/{id}` rather than the admin form,
because #963 is a frontend change that has not reached prod — the backend has
always accepted these fields for Mantle. ⚠️ That endpoint enforces double-submit
CSRF: the `__Host-bff_csrf` cookie is JS-readable and must be echoed in
`X-CSRF-Token`, or it 403s.

⚠️ **History is not corrected.** Pricing snapshots are captured per message at
write time, so prod turns before 17:41Z keep their $0.00 cache cost and
inflated savings. Any gpt-5.4 cost figure quoted for an earlier period is wrong
in both directions.

⚠️ Deleting the row was considered and **rejected**: seven prod assistants
hard-bind it through `modelConfig: {modelId, provider}` and the `staff` role
grants it explicitly, so removing it would strand them.

## Verification

Prove the cost impact rather than assuming it — per the cost-effectiveness tenet:

1. Drive real turns via the dev-ai experiment harness against a GPT-5.6 model with
   a long stable prefix (system prompt + full tool config).
2. Read the session's `C#` rows: `cacheStatus`, `cacheReadInputTokens`,
   `cacheWriteInputTokens`, and the fingerprint hashes. A working config shows
   turn 1 `first_write`, turns 2+ `hit` with a `cacheRead` that tracks the prefix
   and a near-zero `cacheWrite`.
3. Cross-check `GET /admin/costs/sessions/{id}/calls` against the model card rates
   by hand for at least one turn — this is the step that catches a
   double-counted-input regression, which looks like a plausible number rather
   than an error.
4. Compare a Converse-routed control session to confirm the caching delta is real
   and roughly the magnitude estimated above.

## Out of scope

- Migrating `openai.gpt-5.4` off Mantle. It works, it's implicit-only, and it has
  no write fee; PR-1 and PR-3 fix its accounting where it sits.
- Guardrails for GPT-5.6 (Converse-only, and we're deliberately not on Converse).
- Chat Completions on either endpoint — no caching support, no reason.

## Live verification, 2026-09-05

First end-to-end exercise of this work against a real model, via
`backend/scripts/probe_gpt56_cache_rates.py` on `us.openai.gpt-5.6-sol`
(dev-ai, us-west-2). The probe calls the transport directly — it touches no
shared catalog row, no RBAC, and not the agent loop — so it could run while
PR-3 is still blocked.

| Step | Verdict |
|---|---|
| PR-1 usage normalization | ✅ buckets disjoint on every turn of every run |
| PR-2 bedrock-runtime transport | ✅ reached the model; base URL, per-request bearer token and inference-profile id all correct |
| PR-4 explicit breakpoints | ⛔ ~57% more expensive than implicit — shipped OFF |
| PR-5 model-derived TTL | ✅ warm turns read the prefix, so the 30m window is the one that matters |

**The finding that mattered.** Turn 1 of the first run reported
`inputTokens=2, cacheWriteInputTokens=1,446`. Un-normalized, OpenAI reports
`input_tokens=1448` — inclusive of the write bucket. That is PR-1's
cache-write subtraction proving itself on live data: without it that turn
double-bills 1,446 tokens at the input rate *plus* the 1.25× write premium.
It also confirms the `cache_write_tokens` recovery works, since Strands drops
the field and the bucket would otherwise read 0.

**Rates.** The runs consumed ~190k tokens on usage types nothing else in dev
uses, so Cost Explorer attribution is unambiguous. Recover the rates with:

    AWS_PROFILE=dev-ai uv run python scripts/probe_gpt56_cache_rates.py \
        --rates-only --since 2026-09-05

⚠️ Confirm the usage *unit* before trusting the derived $/MTok — Bedrock token
usage types are reported in 1K-token units, and the script's conversion assumes
that. Cross-check one row against the token totals the run printed.
