# Plan: prompt caching for OpenAI GPT-5.6 on Bedrock

**Status:** In progress — PR-1 shipped (#945), PR-2..PR-5 outstanding
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

### PR-4 — Explicit cache breakpoints + `prompt_cache_key`

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

### PR-5 — Observability

- `CACHE_TTL_SECONDS = 300` (`observability/prompt_cache.py:56`) is wrong by 6× for
  OpenAI's 30-minute TTL, so `classify_cache_status` will call
  `miss_ttl_expired` on entries that are still live. Make the TTL model-derived
  rather than a module constant.
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
