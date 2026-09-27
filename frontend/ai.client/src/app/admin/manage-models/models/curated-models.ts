import { ManagedModelFormData, ModelProvider, SupportedParams } from './managed-model.model';

/**
 * A curated entry shown in the model catalog. Carries everything needed to
 * one-click create a fully-configured managed model — including pricing and
 * per-param specs — plus a small amount of presentation metadata for the card.
 *
 * NOTE — **the model card is the primary source for rates.** Each model's page
 * in the Bedrock User Guide publishes its full rate card (per inference option
 * and context window) alongside caching support, context windows, service
 * tiers and endpoint support:
 *
 *   docs.aws.amazon.com/bedrock/latest/userguide/model-card-<provider>-<model>.html
 *
 * Check it first — the cards carry context windows, caching support,
 * parameter tables and cutoffs, none of which any pricing API publishes.
 *
 * **But corroborate the RATES against the Price List API.** As of 2026-09-21
 * it carries almost everything, and a rate worth shipping should agree in
 * both. There are two offer files, and picking the wrong one is the trap:
 *
 *   # Claude, Cohere, Palmyra, TwelveLabs, Luma, Stability  (372 SKUs)
 *   aws pricing get-products --region us-east-1 \
 *     --service-code AmazonBedrockFoundationModels \
 *     --filters Type=TERM_MATCH,Field=regionCode,Value=us-west-2
 *
 *   # Nova, xAI, Google, DeepSeek, Qwen, Moonshot             (1052 SKUs)
 *   aws pricing get-products --region us-east-1 \
 *     --service-code AmazonBedrock \
 *     --filters Type=TERM_MATCH,Field=regionCode,Value=us-west-2
 *
 * ⚠️ An earlier revision of this comment said the API returns "10 Claude
 * SKUs, none newer than Claude 3". **That is no longer true**, and the lesson
 * is the failure mode rather than the fact: a query against the wrong offer
 * file, or one filtering on the now-removed `model` attribute, returns zero
 * and reads exactly like an unpublished model. Re-verified 2026-09-21 — every
 * Claude row below is present and matches its card to the cent (Haiku 4.5
 * $1.10/$5.50 Regional, $1.00/$5.00 Global; Sonnet 4.6 $3.30/$16.50; Opus 4.7
 * $5.50/$27.50; Sonnet 5 $2.00/$10.00 Global; Fable 5.1 $10/$50 Global with a
 * $0.25 cache read that independently confirms its 0.025x multiplier).
 *
 * The product schema now exposes only regionCode, usagetype, location,
 * servicename and operation — there is no `model` and no `tokenType`. Match on
 * `servicename` in the FoundationModels file ("Claude Haiku 4.5 (Amazon
 * Bedrock Edition)") and on `usagetype` in the AmazonBedrock one
 * ("USW2-moonshotai.kimi-k3-mantle-input-tokens-standard").
 *
 * Newer models publish `*_tokens_standard` usagetypes; older ones publish
 * `*TokenCount`. Both shapes are live TODAY in the same file — Sonnet 5 and
 * Opus 4.7 use the first, Haiku 4.5 and Sonnet 4.6 the second — so a query
 * written for one silently returns nothing for the other. Match both.
 *
 * The one real gap: the **hosted OpenAI family** (`openai.gpt-5.4`,
 * `us.openai.gpt-5.[56]*`, `us.openai.gpt-6-*`) is absent from BOTH offer
 * files, so its cards remain the only source. Reading that absence as
 * "unpublished" once put three rows into the dev catalog at GovCloud prices,
 * over-charging by 20%. Note the old explanation for it — "those models bill
 * through AWS Marketplace, which no pricing API covers" — is wrong: Claude's
 * rows are `MP:` Marketplace usagetypes and are covered. Only `gpt-oss`, the
 * open-weight family, appears; the hosted GPT models genuinely do not.
 */
export interface CuratedModel {
  /** Stable key for tracking + tests. Not persisted on the model itself. */
  key: string;
  /** Tagline shown under the model name on the card. */
  tagline: string;
  /** Short capability badges (e.g. 'Extended thinking', 'Vision'). */
  capabilities: string[];
  /**
   * Which AWS pricing tier `template.modelId` actually resolves to. A `us.*` id
   * is a Regional (CRIS) inference profile and prices ~10% ABOVE `global.*` for
   * the same model — the mismatch this field exists to prevent was live for
   * months, with Global rates declared under Regional ids.
   *
   * Not persisted: a curation-time guard, asserted against the id prefix in
   * `model-catalog.page.spec.ts`. Omitted for providers with a single tier.
   */
  pricingTier?: 'regional' | 'global';
  /** Fully-baked template that can be POSTed to /admin/managed-models. */
  template: ManagedModelFormData;
}

const claude4xDefaults = (): Pick<
  ManagedModelFormData,
  | 'provider'
  | 'providerName'
  | 'inputModalities'
  | 'outputModalities'
  | 'responseStreamingSupported'
  | 'maxInputTokens'
  | 'allowedAppRoles'
  | 'availableToRoles'
  | 'enabled'
  | 'isDefault'
  | 'supportsCaching'
> => ({
  provider: 'bedrock',
  providerName: 'Anthropic',
  inputModalities: ['TEXT', 'IMAGE'],
  outputModalities: ['TEXT'],
  responseStreamingSupported: true,
  // ⚠️ 200_000 is correct for Haiku 4.5 ONLY. Every other Claude model we
  // curate has a 1M window, so a new row that does not override this inherits
  // a value that is wrong by 5x — and the failure is silent, because
  // maxInputTokens drives model-relative compaction (it cuts at window *
  // COMPACTION_CEILING_RATIO) and a plausible number looks like a correct one.
  // Sonnet 4.6 and Opus 4.7 both shipped this way; fixed 2026-09-21. Check the
  // AWS model card and declare the window explicitly on every new entry.
  maxInputTokens: 200_000,
  allowedAppRoles: [],
  availableToRoles: [],
  enabled: true,
  isDefault: false,
  supportsCaching: true,
});

/**
 * The cache-read multiple that has held for every model currently in the
 * table. It is a DEFAULT, not a law — see `ratesWithDerivedCache`.
 */
const DEFAULT_CACHE_READ_MULTIPLIER = 0.1;

/**
 * Bedrock publishes cache rates as multiples of a model's base input rate.
 * Cache write is **1.25x** across every family we have checked (the 1-hour
 * Claude write we do not use is 2x), and that one is stable enough to derive:
 * the GPT-5.6 model cards publish the same multiplier (Sol 4.40 -> 5.50), and
 * commercial Cost Explorer billing reproduces it to four decimals on every
 * clean day. Two model families, two independent sources, same ratio.
 *
 * **Cache read is NOT stable at 0.1x and must not be treated as a constant.**
 * It is the default because it holds for every row below, but there are live
 * counterexamples on Bedrock today — Claude Fable 5.1 reads at 0.025x (a 75%
 * cut Anthropic states explicitly) and xAI Grok 4.6 at 0.25x. Both are
 * deliberately absent from this table: encoding a specific rate needs a second
 * independent source, and Grok publishes no cache-write SKU at all (implicit
 * caching only), so there is nothing for our explicit-`cachePoint` contract to
 * place. Pass `cacheReadMultiplier` when a model's card says otherwise, so the
 * next model that breaks the ratio is a data change and not a code change.
 *
 * `input` and `output` are the only independently published numbers, and both
 * are TIER-SPECIFIC. Pass the rates for the tier the `modelId` names, and set
 * `pricingTier` to match; a `us.*` id costs ~10% more than the `global.*` rates
 * for the same model, which is exactly how the two drifted apart before.
 */
const ratesWithDerivedCache = (
  input: number,
  output: number,
  cacheReadMultiplier: number = DEFAULT_CACHE_READ_MULTIPLIER,
): Pick<
  ManagedModelFormData,
  | 'inputPricePerMillionTokens'
  | 'outputPricePerMillionTokens'
  | 'cacheWritePricePerMillionTokens'
  | 'cacheReadPricePerMillionTokens'
> => {
  // Binary floats turn 1.1 * 0.1 into 0.11000000000000001; these are dollar
  // rates that get multiplied by token counts, so pin them to the published
  // precision rather than shipping the artifact into every cost row.
  const round = (n: number): number => Math.round(n * 1e6) / 1e6;
  return {
    inputPricePerMillionTokens: input,
    outputPricePerMillionTokens: output,
    cacheWritePricePerMillionTokens: round(input * 1.25),
    cacheReadPricePerMillionTokens: round(input * cacheReadMultiplier),
  };
};

export const CURATED_BEDROCK_MODELS: CuratedModel[] = [
  {
    key: 'claude-opus-5-5',
    tagline: 'Anthropic\'s most capable model — coding, knowledge work and long-running tasks.',
    capabilities: ['Adaptive thinking', 'Effort control', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...claude4xDefaults(),
      // `us.` because dev's SCP denies every `global.*` profile (verified for
      // this id 2026-09-25). Prod has no SCP: `global.anthropic.claude-opus-5-5`
      // at $4.00 / $20.00 is the cheaper id there.
      modelId: 'us.anthropic.claude-opus-5-5',
      // Card: 1M context, 128K output, June 2026 cutoff.
      maxInputTokens: 1_000_000,
      modelName: 'Claude Opus 5.5',
      shortDescription: 'For your toughest challenges',
      maxOutputTokens: 128_000,
      // Regional (CRIS): $4.40 / $22.00, cache write $5.50 (1.25x) — Price List
      // API, `AmazonBedrockFoundationModels`, 2026-09-25 (the card defers to the
      // pricing page). Cache READ is $0.22, **0.05x** input, not the 0.1x
      // default; the Global rows ($4.00 / $0.20) show the same ratio.
      ...ratesWithDerivedCache(4.4, 22.0, 0.05),
      knowledgeCutoffDate: '2026-06-01',
      // Measured against `us.anthropic.claude-opus-5-5` 2026-09-25: temperature
      // and top_p both 400 ("deprecated for this model"), and
      // `thinking.type.disabled` 400s — adaptive thinking is always on, so no
      // `thinking` param is declared. The effort enum is the endpoint's own
      // 400 on a bogus level; `medium` is the card's published default.
      supportedParams: {
        params: {
          max_tokens: { supported: true, min: 1, max: 128_000, default: 32_000 },
          effort: {
            supported: true,
            allowed: ['low', 'medium', 'high', 'xhigh', 'max'],
            default: 'medium',
          },
        },
      },
    },
  },
  {
    key: 'claude-opus-4-7',
    tagline: 'Previous-generation Opus — for the hardest reasoning.',
    capabilities: ['Adaptive thinking', 'Effort control', 'Vision', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-opus-4-7',
      // 1M per the AWS model card (verified 2026-09-21). WITHOUT this the row
      // inherits claude4xDefaults()'s 200_000 and model-relative compaction
      // cuts at 100k instead of 500k — five times earlier than the model
      // needs, paying a prefix re-write and a summarizer call each time.
      maxInputTokens: 1_000_000,
      modelName: 'Claude Opus 4.7',
      shortDescription: 'Previous-generation Opus',
      // Superseded by Claude Opus 5.5, which is also cheaper.
      isFeatured: false,
      maxOutputTokens: 64_000,
      // Regional (CRIS): $5.50 / $27.50. Global is $5.00 / $25.00.
      ...ratesWithDerivedCache(5.5, 27.5),
      knowledgeCutoffDate: '2025-10-01',
      supportedParams: {
        params: {
          max_tokens: { supported: true, min: 1, max: 64_000, default: 32_000 },
          effort: {
            supported: true,
            allowed: ['low', 'medium', 'high', 'xhigh', 'max'],
            default: 'medium',
          },
        },
      },
    },
  },
  {
    key: 'claude-sonnet-5',
    tagline: 'Anthropic\'s Sonnet 5 — 1M-token context with effort-based reasoning.',
    capabilities: ['Effort control', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'global',
    template: {
      ...claude4xDefaults(),
      modelId: 'global.anthropic.claude-sonnet-5',
      modelName: 'Claude Sonnet 5',
      shortDescription: 'Strong reasoning over very long context',
      maxInputTokens: 1_000_000,
      maxOutputTokens: 128_000,
      // Global: $2.00 / $10.00 — correct as declared, this id really is
      // `global.*`. Regional would be $2.20 / $11.00.
      ...ratesWithDerivedCache(2.0, 10.0),
      knowledgeCutoffDate: null,
      supportedParams: {
        params: {
          max_tokens: { supported: true, min: 1, max: 128_000, default: 128_000 },
          effort: {
            supported: true,
            allowed: ['low', 'medium', 'high', 'xhigh'],
            default: 'medium',
          },
        },
      },
    },
  },
  {
    key: 'claude-sonnet-4-6',
    tagline: 'Balanced reasoning model — Anthropic\'s default workhorse.',
    capabilities: ['Extended thinking', 'Vision', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-sonnet-4-6',
      // 1M per the AWS model card (verified 2026-09-21) — same inherited-200k
      // trap as the Opus 4.7 row above.
      maxInputTokens: 1_000_000,
      modelName: 'Claude Sonnet 4.6',
      shortDescription: 'Balanced reasoning for everyday work',
      // Superseded by Claude Sonnet 5 in this same catalog.
      isFeatured: false,
      maxOutputTokens: 64_000,
      // Regional (CRIS): $3.30 / $16.50. Global is $3.00 / $15.00.
      ...ratesWithDerivedCache(3.3, 16.5),
      knowledgeCutoffDate: '2025-07-01',
      supportedParams: {
        params: {
          temperature: { supported: true, min: 0, max: 1, default: 0.7 },
          top_p: { supported: true, min: 0, max: 1, default: null },
          top_k: { supported: true, min: 1, default: null },
          max_tokens: { supported: true, min: 1, max: 64_000, default: 8192 },
          thinking: { supported: true, min: 1024, max: 48_000, default: 4096 },
        },
      },
    },
  },
  {
    key: 'claude-haiku-4-5',
    tagline: "Anthropic's fastest model — great for high-throughput tasks.",
    capabilities: ['Extended thinking', 'Vision', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
      modelName: 'Claude Haiku 4.5',
      shortDescription: 'Fastest for quick answers',
      maxOutputTokens: 64_000,
      // Regional (CRIS): $1.10 / $5.50. Global is $1.00 / $5.00. This is the
      // platform default model, so this is the row every cost number rides on.
      ...ratesWithDerivedCache(1.1, 5.5),
      knowledgeCutoffDate: '2025-02-01',
      supportedParams: {
        params: {
          temperature: { supported: true, min: 0, max: 1, default: 1.0 },
          top_p: { supported: true, min: 0, max: 1, default: null },
          top_k: { supported: true, min: 1, default: null },
          max_tokens: { supported: true, min: 1, max: 64_000, default: 8192 },
          thinking: { supported: true, min: 1024, max: 32_000, default: 4096 },
        },
      },
    },
  },
];

/**
 * Shared defaults for Bedrock Mantle (OpenAI-compatible open-weight) models.
 *
 * `supportsCaching: false` is the right DEFAULT here — most Mantle models are
 * open-weight and genuinely never cache — but it is not universal, so any
 * entry for a model that does cache must override it. `openai.gpt-5.4` is the
 * one below that does: its model card publishes a cache-read rate (0.1x input)
 * with no write fee. Inheriting the default there priced its cached tokens at
 * $0.00 while AWS billed them, which is exactly the bug that had to be fixed
 * by hand in prod. `apiMode` (Chat Completions vs Responses) and an optional `region`
 * are the Mantle-specific fields — sourced from each model card (there is no
 * API that exposes them). The base path is derived by the SDK from the model id.
 */
const mantleDefaults = (): Pick<
  ManagedModelFormData,
  | 'provider'
  | 'outputModalities'
  | 'responseStreamingSupported'
  | 'allowedAppRoles'
  | 'availableToRoles'
  | 'enabled'
  | 'isDefault'
  | 'supportsCaching'
> => ({
  provider: 'mantle',
  outputModalities: ['TEXT'],
  responseStreamingSupported: true,
  allowedAppRoles: [],
  availableToRoles: [],
  enabled: true,
  isDefault: false,
  supportsCaching: false,
});

// Pricing verified against the AWS Bedrock pricing page (2026-06); modalities,
// capabilities, context, and endpoint path verified against each model card.
// Mantle per-token pricing equals the bedrock-runtime price for the same model.
// Re-verify when AWS revises pricing or a newer model version ships.
export const CURATED_MANTLE_MODELS: CuratedModel[] = [
  {
    key: 'gpt-5-4',
    tagline: 'OpenAI GPT-5.4 on Bedrock Mantle — multimodal reasoning via the Responses API.',
    capabilities: ['Reasoning', 'Vision', 'Long context'],
    template: {
      ...mantleDefaults(),
      modelId: 'openai.gpt-5.4',
      modelName: 'GPT-5.4',
      shortDescription: 'Multimodal reasoning',
      providerName: 'OpenAI',
      inputModalities: ['TEXT', 'IMAGE'],
      maxInputTokens: 272_000,
      maxOutputTokens: 128_000,
      // GPT-5.x is served on Mantle's `/openai/v1` base path and requires the
      // Responses API. Its `openai.gpt-5.*` model id matches the SDK's
      // _OPENAI_PATH_MODEL_PREFIXES, so one-click create routes correctly
      // (unlike the Gemma case noted below).
      apiMode: 'responses',
      // Model card, In-Region: $2.75 / $16.50, cache read $0.275 (0.1x input),
      // and the cache-write cell is an em dash — there is NO write fee on this
      // model. A literal 0 is the correct rate, not a missing value: it makes
      // `compute_wasted_usd` see a non-positive premium and return $0 instead
      // of inventing waste.
      inputPricePerMillionTokens: 2.75,
      outputPricePerMillionTokens: 16.5,
      supportsCaching: true,
      cacheReadPricePerMillionTokens: 0.275,
      cacheWritePricePerMillionTokens: 0,
    },
  },
  {
    key: 'qwen3-coder-30b',
    tagline: 'Qwen3 Coder 30B — long-context coding model on Bedrock Mantle.',
    capabilities: ['Coding', 'Long context'],
    template: {
      ...mantleDefaults(),
      modelId: 'qwen.qwen3-coder-30b-a3b-instruct',
      modelName: 'Qwen3 Coder 30B',
      shortDescription: 'Long-context coding',
      // Specialist coding model, not a general chat default.
      isFeatured: false,
      providerName: 'Qwen',
      inputModalities: ['TEXT'],
      maxInputTokens: 256_000,
      maxOutputTokens: 8_192,
      apiMode: 'chat',
      inputPricePerMillionTokens: 0.15,
      outputPricePerMillionTokens: 0.6,
      supportedParams: {
        params: {
          temperature: { supported: true, min: 0, max: 2, default: 0.7 },
          top_p: { supported: true, min: 0, max: 1, default: null },
          max_tokens: { supported: true, min: 1, max: 8_192, default: 4_096 },
        },
      },
    },
  },
  // NOTE: Gemma 4 (`google.gemma-4-*`) is served ONLY on Mantle's `/openai/v1`
  // base path (per its AWS model card — different from the `/v1` path Gemma 3
  // and gpt-oss use). The Strands SDK's _OPENAI_PATH_MODEL_PREFIXES ships only
  // `openai.gpt-5.`, so the backend appends `google.gemma-4-` at build time
  // (see apis/shared/models/mantle.py::_ensure_gemma4_openai_v1_routing) until
  // it lands upstream. That bridge makes a one-click Gemma 4 card route
  // correctly, so this is safe to curate — pending confirmed pricing/params.
  // Use the `google.gemma-4-` prefix, NOT `google.gemma-`: Gemma 3 is on `/v1`.
];


/**
 * Shared defaults for `bedrock-responses` — the OpenAI **Responses** API on
 * `bedrock-runtime`.
 *
 * `supportsCaching: true` is not a preference here, it is the only truthful
 * value. These models cache implicitly and server-side with no way to turn it
 * off, so `false` would be a false statement whose only effect is to clear the
 * cache-rate fields — pricing cached tokens at $0.00 while AWS bills them in
 * full. On a warm conversation nearly every input token is a cached one, so
 * that is close to total under-reporting. The backend normalizes it the same
 * way (`_resolve_supports_caching`, forced for this provider), as it does
 * `apiMode: 'responses'`.
 *
 * `maxInputTokens: 272_000` is **load-bearing pricing**, not just a cap. These
 * models have a 1M window, but AWS prices them on two cards: above 272K, input
 * costs 2x and output 1.5x. `CuratedModel` holds one flat rate per bucket, so
 * the cap is what keeps that single rate correct. Raising it silently opens
 * the second price card and under-charges every long turn.
 */
const bedrockResponsesDefaults = (): Pick<
  ManagedModelFormData,
  | 'provider'
  | 'providerName'
  | 'inputModalities'
  | 'outputModalities'
  | 'responseStreamingSupported'
  | 'maxInputTokens'
  | 'maxOutputTokens'
  | 'allowedAppRoles'
  | 'availableToRoles'
  | 'enabled'
  | 'isDefault'
  | 'supportsCaching'
  | 'apiMode'
> => ({
  provider: 'bedrock-responses',
  providerName: 'OpenAI',
  inputModalities: ['TEXT', 'IMAGE'],
  outputModalities: ['TEXT'],
  responseStreamingSupported: true,
  maxInputTokens: 272_000,
  // The cards publish no output cap ("Max output tokens: N/A"), so claim none
  // rather than invent one — this value is only a ceiling on the configured
  // max_tokens param and is never sent to the provider.
  maxOutputTokens: null,
  allowedAppRoles: [],
  availableToRoles: [],
  enabled: true,
  isDefault: false,
  supportsCaching: true,
  apiMode: 'responses',
});

/**
 * The OpenAI family on `bedrock-runtime` via the Responses API.
 *
 * Rates are the **Geo CRIS, Short Context (272K)** row from each model card —
 * Geo CRIS is the tier the `us.*` inference profiles resolve to, and these
 * models are inference-profile-only (no ON_DEMAND). Verified 2026-09-06,
 * re-verified for GPT-6 Astra 2026-09-11.
 *
 * **Every card in this family publishes two price tables, and the tier is
 * selected by the ACTUAL token count of the request** — not by a declared
 * window, and not by a separate model id. Settled 2026-09-11 (see
 * `docs/kaizen/review-queue.md`): the Price List API encodes `-long-ctx` as a
 * value of `tokenType` under an identical `model` attribute, GPT-5.6 Terra
 * declares a 1M window against a single model id yet still publishes a
 * reachable Short Context table, and our own Cost Explorer rows bill
 * `_standard` and never `-long-ctx` while calling those 1M-window ids. There
 * is no field in which a window could be declared: `maxInputTokens` below is
 * ours, is read only for compaction and telemetry, and never reaches a
 * Bedrock request.
 *
 * That is exactly why `maxInputTokens: 272_000` is load-bearing. It does not
 * protect us from being over-charged — nothing here does. It keeps the single
 * flat rate per bucket that `CuratedModel` can hold **arithmetically true**,
 * because a request that crosses 272K bills input at 2x, output at 1.5x and
 * both cache buckets at 2x. Raising it silently UNDER-charges every long turn.
 *
 * `supportedParams` was deliberately absent here until 2026-09-12, when it was
 * MEASURED — the bar this comment has always set. AWS still publishes no
 * parameter table for these models (`model-parameters-openai.html` documents
 * only the open-weight gpt-oss family), so the evidence is the endpoint's own
 * responses, probed against all four ids in us-west-2 (and re-probed, with
 * identical results, against GPT-6 Sol, GPT-6 Luna and GPT-5.5 on 2026-09-25):
 *
 *   - `reasoning.effort` — sending a deliberately invalid value returns a 400
 *     that ENUMERATES the enum: "Supported values are: 'none', 'low',
 *     'medium', 'high', 'xhigh', and 'max'." Identical on Sol, Terra, Luna and
 *     Astra, and it matches the launch blog ("They also support none, low,
 *     medium, high, xhigh, and max reasoning effort"). Two independent sources.
 *   - `temperature` and `top_p` — hard 400 on all four: "Unsupported
 *     parameter: 'temperature' is not supported with this model."
 *   - `max_output_tokens` — accepted.
 *
 * That last pair is why declaring a spec here is a FIX, not just an enabler.
 * With no spec the #915 guard stays permissive, so a `temperature` reaching
 * this family from any caller that can set one kills the turn with a 400
 * mid-stream. Declaring them `supported: false` drops them before the request
 * instead — the same failure class the guard inversion was written to close on
 * Claude Opus 4.7.
 *
 * `reasoning_effort` defaults to `medium`, which was also measured rather than
 * assumed. Neither the cards nor the blog publish a default, so the question
 * was what the provider does when the param is ABSENT — which is not the same
 * as sending `none`. Sending nothing still reasons; `none` is an explicit
 * "off". Three samples per level on Luna, in reasoning tokens:
 *
 *     unset  285 / 516 / 327   (mean 376)
 *     none     0 /   0 /   0
 *     low    221 / 222 / 230   (mean 224)
 *     medium 274 / 346 / 303   (mean 308)
 *     high   516 / 363 / 497   (mean 459)
 *
 * So the implicit default already sits around medium, and declaring `medium`
 * is cost-neutral-to-slightly-cheaper (-18% reasoning tokens), NOT an increase.
 * It is declared anyway because otherwise the provider can move its own
 * default and our spend follows with no code change and no signal — and
 * because a declared default is what lets the picker show the level in force
 * instead of a blank row. Counts are noisy (unset spanned 285-516 across three
 * identical calls), so treat the middle levels as roughly interchangeable.
 *
 * The level that actually moves the bill is `max`: ~2.3x the output tokens of
 * unset on Terra, and reasoning bills as output. It stays in `allowed`
 * deliberately — dropping a level from that list is the lever if the exposure
 * is ever unwanted, since the picker and the backend both read it.
 *
 * GPT-6 Astra inherits this default by sharing the helper. Its effort ENUM was
 * probed directly, but its token counts were not — medium there is an
 * extrapolation from its GPT-5.6 siblings, not a measurement.
 *
 * The standing rule is unchanged — a declared spec flips the guard from
 * permissive to restrictive, so a wrong entry silently blocks a parameter the
 * model really accepts. Add or widen one only from published or measured
 * evidence, and re-probe rather than assume when a new sibling ships.
 */
/**
 * Measured Responses-API parameter profile for the OpenAI family on
 * `bedrock-runtime`. See the block comment on
 * {@link CURATED_BEDROCK_RESPONSES_MODELS} for the probe and its evidence.
 *
 * `temperature` / `top_p` are declared `supported: false` rather than omitted:
 * omission drops them too (an authoritative spec treats silence as
 * unsupported), but an explicit false records the measured fact and logs the
 * clearer "unsupported inference param" line when one is dropped.
 */
const openaiResponsesParams = (maxOutputTokens: number | null = null): SupportedParams => ({
  params: {
    reasoning_effort: {
      supported: true,
      allowed: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
      // Pins what the provider was already doing implicitly — see the block
      // comment above for the measurement. NOT an increase in reasoning.
      default: 'medium',
    },
    max_tokens: {
      supported: true,
      min: 1,
      ...(maxOutputTokens === null ? {} : { max: maxOutputTokens }),
    },
    temperature: { supported: false },
    top_p: { supported: false },
  },
});

/**
 * Moonshot AI's Kimi K3, curated onto `bedrock-responses` rather than `bedrock`.
 *
 * It is a `bedrock-runtime` model that Converse can call, so the obvious home
 * is `CURATED_BEDROCK_MODELS` alongside Claude. Its model card rules that out
 * on three counts, and names our own framework while doing it:
 *
 *   1. **Converse breaks multi-turn.** "a failure (`InternalServerException`)
 *      when reasoning content from earlier turns is included in a multi-turn
 *      request, which affects frameworks such as LangChain and Strands Agents
 *      in their default configurations." That is our agent loop, in its
 *      default configuration, on turn two.
 *   2. **Converse gets no explicit caching.** Explicit prompt caching is
 *      "Responses and Chat Completions APIs only" — a `bedrock` row would
 *      route over Converse and reach only implicit caching, and our whole
 *      `cachePoint` contract is Converse-shaped and would place nothing.
 *   3. **Converse rejects documents.** "rejection of attached document inputs
 *      such as PDF and HTML." Attachments are ~31% of prod spend; a model that
 *      silently refuses them is not a general chat default.
 *
 * `bedrock-mantle` is not an option either — the card's endpoint table marks it
 * unsupported, unlike the `openai.*` and `qwen.*` rows in
 * {@link CURATED_MANTLE_MODELS}.
 *
 * That leaves `bedrock-responses`, which is where it belongs anyway: the same
 * transport, the same per-request bearer mint, and the same implicit caching
 * the GPT-5.6 family already rides. `supportsCaching: true` is inherited and
 * correct — Kimi K3 caches implicitly by default with no way to turn it off,
 * so `false` would zero the cache rates while AWS billed them.
 *
 * **Two family defaults are deliberately overridden**, and both are pricing:
 *
 * - `maxInputTokens: 1_000_000`. The 272K pin on
 *   {@link bedrockResponsesDefaults} is load-bearing *for the OpenAI family*,
 *   which publishes two price cards selected by actual token count. Kimi K3
 *   publishes ONE table per inference option — there is no second card to fall
 *   into — so the single flat rate stays arithmetically true across the whole
 *   1M window, and inheriting 272K would only compact early for no reason.
 * - `providerName: 'Moonshot AI'`. The family default is `'OpenAI'`, which is
 *   the transport's origin, not this model's vendor.
 *
 * ⚠️ **Cost note on images.** The card: the `detail` parameter that trades
 * image fidelity for cost "is honored only on the Chat Completions API. On the
 * Responses API, images are always processed at high detail." This row routes
 * over Responses (the provider forces `apiMode`), so every image is billed at
 * high detail with no lever. Text and cached prefixes are unaffected.
 *
 * ⚠️ **Caching needs the explicit path on this model, and that is a backend
 * carve-out, not a field here.** Its card says it "supports implicit
 * (automatic) prompt caching" by default. Measured clean-room 2026-09-21
 * (dev-ai, unique prefix per arm, 10.5k prefix, 4 turns) the default bills a
 * cache WRITE every turn and reads one back never — $0.17338, against $0.13872
 * for the same turns UNCACHED. Stock caching is 25% worse than no caching.
 * Full explicit (`prompt_cache_options` + a breakpoint) costs $0.05401, a 69%
 * saving. `_EXPLICIT_CACHE_REQUIRED_MODELS` in
 * `apis/shared/models/bedrock_responses.py` forces that path for this model id
 * regardless of the transport-wide env flag, which stays off for GPT-5.6 where
 * explicit measured 57% WORSE. The rates below are only correct because of it.
 *
 * **`us.` not `global.`, and that is an environment constraint, not a
 * preference.** Global CRIS is cheaper ($3.00 / $15.00 vs $3.30 / $16.50), but
 * dev-ai sits under Control Tower SCP `p-r61tynkc`, which explicitly denies
 * `bedrock:InvokeModel` on EVERY `global.*` profile — verified 2026-09-21
 * against `global.anthropic.claude-haiku-4-5` and `global.anthropic.claude-sonnet-5`
 * as well as this model, while the `us.*` sibling of each works. Curating the
 * Global id would put a model in the picker that 401s on first use in dev. The
 * prod account is in no organization, so no SCP applies there — if this row is
 * ever promoted, `global.moonshotai.kimi-k3` at $3.00 / $15.00 is the cheaper
 * and correct id.
 *
 * Rates are the **US CRIS** column: $3.30 / $16.50, cache read $0.33 (0.1x)
 * and cache write $4.125 (1.25x) — both exactly the derived multipliers, so
 * {@link ratesWithDerivedCache} carries it without an override. **Two
 * independent sources agree to the cent**: the model card, and the Price List
 * API's `AmazonBedrock` offer file (`USW2-moonshotai.kimi-k3-mantle-*-standard`
 * usagetypes). Note the second one is the `AmazonBedrock` service code, NOT
 * `AmazonBedrockFoundationModels` — Moonshot, xAI, Google and Nova all live
 * there, and a query against the other offer file returns nothing for them.
 */
export const CURATED_BEDROCK_RESPONSES_MODELS: CuratedModel[] = [
  {
    key: 'gpt-6-astra',
    tagline: 'Frontier model for the hardest end-to-end work — reasoning, coding and research.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      // UNVERIFIED, and the one thing here worth re-checking before anyone
      // banks cache savings on this row: Astra's card lists Implicit and
      // Explicit Prompt Caching under `bedrock-mantle` ONLY. Caching appears
      // in neither column of its `bedrock-runtime` feature table, where every
      // GPT-5.6 card lists it under both endpoints — and this row routes over
      // `bedrock-runtime`. `supportsCaching` still inherits `true`, which is
      // the safe stance either way: `false` would zero the cache-rate fields
      // and price cached tokens at $0.00 while AWS bills them in full. If
      // caching turns out not to fire here the cost is a stale capability
      // chip, not a mispriced bill.
      modelId: 'us.openai.gpt-6-astra',
      modelName: 'GPT-6 Astra',
      shortDescription: 'Frontier reasoning, coding and research',
      // Geo CRIS Short Context: $11.00 / $55.00. Global CRIS is $10.00 /
      // $50.00. Long Context (1.05M) would be $22.00 / $82.50 — unreachable
      // while maxInputTokens stays pinned at the 272K boundary.
      ...ratesWithDerivedCache(11.0, 55.0),
      // Astra departs from its GPT-5.6 siblings here: its card publishes a
      // real cap (`Max output tokens: 128,000`) where theirs say N/A, so this
      // overrides the family default of `null`. Declaring the published
      // number beats inheriting "unknown".
      maxOutputTokens: 128_000,
      // Published on the card as April 30, 2026 — again unlike the GPT-5.6
      // cards, which state none.
      knowledgeCutoffDate: '2026-04-30',
      supportedParams: openaiResponsesParams(128_000),
    },
  },
  {
    key: 'gpt-6-sol',
    tagline: 'Built for demanding development work — features, debugging, refactors and review.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-6-sol',
      modelName: 'GPT-6 Sol',
      shortDescription: 'Strong coding and agentic work',
      // Card, US Geo CRIS Short Context: $2.20 / $11.00, cache write $2.75,
      // cache read $0.22. Global CRIS is $2.00 / $10.00. Cheaper than the
      // GPT-5.6 Sol and Terra rows it supersedes.
      ...ratesWithDerivedCache(2.2, 11.0),
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'gpt-6-luna',
    tagline: 'Fast and affordable — summaries, extraction and focused questions at scale.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-6-luna',
      modelName: 'GPT-6 Luna',
      shortDescription: 'Fast and affordable, for focused tasks',
      // Card, US Geo CRIS Short Context: $0.11 / $0.55, cache write $0.1375,
      // cache read $0.011. Global CRIS is $0.10 / $0.50.
      ...ratesWithDerivedCache(0.11, 0.55),
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'gpt-5-5',
    tagline: 'OpenAI GPT-5.5 — coding, research and long-running agentic work.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      // The card lists `bedrock-mantle` only, In-Region in us-east-1/2. That is
      // stale: `us.openai.gpt-5.5` is an ACTIVE profile in us-west-2 and
      // answered over bedrock-runtime Responses on 2026-09-25, while Mantle
      // us-west-2 404s the model. So it rides this transport like its siblings.
      modelId: 'us.openai.gpt-5.5',
      modelName: 'GPT-5.5',
      shortDescription: 'Previous-generation GPT',
      // A June 2026 model, older than the GPT-6 family and 2.5x GPT-6 Sol's
      // price — available, but not a picker default.
      isFeatured: false,
      // ⚠️ SINGLE SOURCE. The card publishes only In-Region: $5.50 / $33.00,
      // cache read $0.55, and an em dash for cache write. No US Geo CRIS row is
      // published, and the hosted OpenAI family is in neither Price List offer
      // file. Every sibling card prices US Geo CRIS identically to Mantle
      // In-Region (both carry the same 10% premium), so these are that row.
      // The write rate is a literal 0, as on GPT-5.4: measured 2026-09-25, a
      // 5.9k-token prefix read back 5,470 cached tokens on turns 2 and 3 and
      // reported `cache_write_tokens: 0` on every turn — there is no write
      // bucket to bill. Audit against Cost Explorer on a single-model day.
      inputPricePerMillionTokens: 5.5,
      outputPricePerMillionTokens: 33.0,
      cacheReadPricePerMillionTokens: 0.55,
      cacheWritePricePerMillionTokens: 0,
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'gpt-5-6-sol',
    tagline: 'Previous-generation Sol — frontier reasoning and agentic work.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-5.6-sol',
      modelName: 'GPT-5.6 Sol',
      shortDescription: 'Strong reasoning and agentic work',
      // Superseded by GPT-6 Sol, which is half the price.
      isFeatured: false,
      // Geo CRIS: $4.40 / $22.00. Global CRIS is $4.00 / $20.00.
      ...ratesWithDerivedCache(4.4, 22.0),
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'gpt-5-6-terra',
    tagline: 'Balanced everyday model — strong performance per dollar.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-5.6-terra',
      modelName: 'GPT-5.6 Terra',
      shortDescription: 'Balanced performance per dollar',
      // GPT-6 Sol undercuts it ($2.20 / $11.00 against $2.20 / $13.20), so
      // there is no price tier left for Terra to hold at the top level.
      isFeatured: false,
      // Geo CRIS: $2.20 / $13.20. Global CRIS is $2.00 / $12.00.
      ...ratesWithDerivedCache(2.2, 13.2),
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'gpt-5-6-luna',
    tagline: 'Fast and affordable — for classification, routing and high volume.',
    capabilities: ['Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-5.6-luna',
      modelName: 'GPT-5.6 Luna',
      shortDescription: 'Fast and affordable, for high volume',
      // Its own card pitches it for classification, routing and high
      // volume — a batch workhorse rather than a chat default.
      isFeatured: false,
      // Geo CRIS: $0.22 / $1.32. Global CRIS is $0.20 / $1.20.
      ...ratesWithDerivedCache(0.22, 1.32),
      knowledgeCutoffDate: null,
      supportedParams: openaiResponsesParams(),
    },
  },
  {
    key: 'kimi-k3',
    tagline: 'Moonshot AI\'s open-weight frontier model — 1M-token context with native vision.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      // `us.` because dev's SCP denies every `global.*` profile — see above.
      modelId: 'us.moonshotai.kimi-k3',
      modelName: 'Kimi K3',
      shortDescription: 'Long-context coding and knowledge work',
      providerName: 'Moonshot AI',
      // One price table, so the whole 1M window bills at one rate — see the
      // block comment above for why this override is safe here and is NOT on
      // the OpenAI siblings.
      maxInputTokens: 1_000_000,
      // US CRIS: $3.30 / $16.50, cache read $0.33, cache write $4.125.
      // Global CRIS would be $3.00 / $15.00 — unreachable under dev's SCP.
      ...ratesWithDerivedCache(3.3, 16.5),
      // The card publishes no output cap and no knowledge cutoff. Claim
      // neither rather than invent one.
      maxOutputTokens: null,
      knowledgeCutoffDate: null,
      // MEASURED against `us.moonshotai.kimi-k3` in us-west-2 on 2026-09-21,
      // not inherited: `openaiResponsesParams` declares temperature and top_p
      // unsupported, and on this model temperature is ACCEPTED. Borrowing the
      // OpenAI spec would have silently stripped a working parameter.
      supportedParams: {
        params: {
          // 400 on 2: "This model accepts 'temperature' between 0 and 1."
          // (Note the API schema itself allows <= 2 — the 2.1 rejection quotes
          // the schema bound, the 2.0 rejection quotes the model's. The
          // model's is the real one.) No default is published, so claim none.
          temperature: { supported: true, min: 0, max: 1, default: null },
          // Measured 400 on 0.9: "This model accepts 'top_p' only with the
          // value 0.95." A parameter whose sole legal value is what you get by
          // omitting it is not a knob. Declared false so the request never
          // carries it — `supported: true` would let a picker send 0.7 and
          // kill the turn with a 400 mid-stream.
          top_p: { supported: false },
          // 400 on 1: "Expected a value >= 16, but got 1 instead." The 16 is
          // measured; the family's usual `min: 1` would be a guaranteed 400.
          // The card publishes no cap, so declare none.
          max_tokens: { supported: true, min: 16 },
          // Enumerated by the endpoint's own 400 on a bogus level: "Supported
          // values are: 'none', 'low', 'medium', 'high', 'xhigh', and 'max'."
          // Same enum as the OpenAI family, arrived at independently.
          reasoning_effort: {
            supported: true,
            allowed: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
            // Pins what the provider already does implicitly, as on the GPT-5.6
            // rows. Three samples each, reasoning tokens: unset 114/111/95
            // (mean 107), low 73/93/60 (mean 75), medium 142/108/89 (mean 113).
            // Unset already sits at medium, so this is cost-neutral, not an
            // increase — and it stops the provider moving its own default with
            // our spend following silently. `none` returned 0 reasoning tokens
            // and is the lever if that exposure is ever unwanted.
            default: 'medium',
          },
        },
      },
    },
  },
];

/**
 * Provider-keyed lookup for the catalog tabs. Bedrock, Mantle and
 * bedrock-responses are populated; OpenAI/Gemini are intentional empty arrays
 * — the page renders a 'Coming soon' empty state when the active tab has no
 * entries.
 */
export const CURATED_MODELS_BY_PROVIDER: Record<ModelProvider, CuratedModel[]> = {
  bedrock: CURATED_BEDROCK_MODELS,
  openai: [],
  gemini: [],
  mantle: CURATED_MANTLE_MODELS,
  'bedrock-responses': CURATED_BEDROCK_RESPONSES_MODELS,
};
