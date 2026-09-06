import { ManagedModelFormData, ModelProvider } from './managed-model.model';

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
 * Check it first. The OpenAI-family rates are absent from the Price List API
 * entirely — those models bill through AWS Marketplace, which no pricing API
 * covers — and reading that absence as "unpublished" put three rows into the
 * dev catalog at GovCloud prices, over-charging by 20%.
 *
 * Claude rates below were read from the **AWS Price List API**
 * (`AmazonBedrockFoundationModels`, us-west-2, published 2026-09-01), which
 * does carry them. Re-verify there when bumping a model id:
 *
 *   aws pricing get-products --region us-east-1 \
 *     --service-code AmazonBedrockFoundationModels \
 *     --filters Type=TERM_MATCH,Field=regionCode,Value=us-west-2
 *
 * Newer models publish `*_tokens_standard` usagetypes; older ones publish
 * `*TokenCount`. A query written for one shape silently returns nothing for
 * the other — match both, or a model looks unpriced when it is merely renamed.
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
  maxInputTokens: 200_000,
  allowedAppRoles: [],
  availableToRoles: [],
  enabled: true,
  isDefault: false,
  supportsCaching: true,
});

/**
 * Bedrock publishes cache rates as fixed multiples of a model's base input
 * rate: cache write is **1.25x**, cache read is **0.1x** (the 1-hour Claude
 * write we do not use is 2x). Deriving them removes the two fields most likely
 * to drift — the ratios were the one thing the old table got right.
 *
 * Not Claude-specific: the GPT-5.6 model cards publish exactly the same two
 * multipliers (Sol 4.40 -> 5.50 / 0.44), and commercial Cost Explorer billing
 * reproduces them to four decimals on every clean day. Two model families, two
 * independent sources, same ratios.
 *
 * `input` and `output` are the only independently published numbers, and both
 * are TIER-SPECIFIC. Pass the rates for the tier the `modelId` names, and set
 * `pricingTier` to match; a `us.*` id costs ~10% more than the `global.*` rates
 * for the same model, which is exactly how the two drifted apart before.
 */
const ratesWithDerivedCache = (
  input: number,
  output: number,
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
    cacheReadPricePerMillionTokens: round(input * 0.1),
  };
};

export const CURATED_BEDROCK_MODELS: CuratedModel[] = [
  {
    key: 'claude-opus-4-7',
    tagline: 'Anthropic\'s most capable model — for the hardest reasoning.',
    capabilities: ['Adaptive thinking', 'Effort control', 'Vision', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-opus-4-7',
      modelName: 'Claude Opus 4.7',
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
      modelName: 'Claude Sonnet 4.6',
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
 * GPT-5.6 on `bedrock-runtime` via the Responses API.
 *
 * Rates are the **Geo CRIS, Short Context (272K)** row from each model card —
 * Geo CRIS is the tier the `us.*` inference profiles resolve to, and these
 * models are inference-profile-only (no ON_DEMAND). Verified 2026-09-06.
 *
 * `supportedParams` is deliberately absent. AWS publishes no parameter table
 * for these models (`model-parameters-openai.html` documents only the
 * open-weight gpt-oss family), and an invented spec would be worse than none:
 * a declared spec flips the #915 guard from permissive to restrictive, so a
 * wrong entry silently blocks a parameter the model actually accepts. Add one
 * only from published or measured evidence.
 */
export const CURATED_BEDROCK_RESPONSES_MODELS: CuratedModel[] = [
  {
    key: 'gpt-5-6-sol',
    tagline: 'OpenAI\'s most capable model — frontier reasoning and agentic work.',
    capabilities: ['Reasoning', 'Vision', 'Long context', 'Prompt caching'],
    pricingTier: 'regional',
    template: {
      ...bedrockResponsesDefaults(),
      modelId: 'us.openai.gpt-5.6-sol',
      modelName: 'GPT-5.6 Sol',
      // Geo CRIS: $4.40 / $22.00. Global CRIS is $4.00 / $20.00.
      ...ratesWithDerivedCache(4.4, 22.0),
      knowledgeCutoffDate: null,
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
      // Geo CRIS: $2.20 / $13.20. Global CRIS is $2.00 / $12.00.
      ...ratesWithDerivedCache(2.2, 13.2),
      knowledgeCutoffDate: null,
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
      // Geo CRIS: $0.22 / $1.32. Global CRIS is $0.20 / $1.20.
      ...ratesWithDerivedCache(0.22, 1.32),
      knowledgeCutoffDate: null,
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
