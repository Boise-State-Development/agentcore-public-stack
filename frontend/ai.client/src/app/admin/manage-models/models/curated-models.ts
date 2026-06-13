import { ManagedModelFormData, ModelProvider } from './managed-model.model';

/**
 * A curated entry shown in the model catalog. Carries everything needed to
 * one-click create a fully-configured managed model — including pricing and
 * per-param specs — plus a small amount of presentation metadata for the card.
 *
 * NOTE — Pricing and inference-profile IDs reflect Anthropic's published
 * pricing as of 2026-05. Verify against AWS Bedrock + Anthropic docs before
 * merging this PR, and bump the IDs when newer model versions ship.
 */
export interface CuratedModel {
  /** Stable key for tracking + tests. Not persisted on the model itself. */
  key: string;
  /** Tagline shown under the model name on the card. */
  tagline: string;
  /** Short capability badges (e.g. 'Extended thinking', 'Vision'). */
  capabilities: string[];
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

export const CURATED_BEDROCK_MODELS: CuratedModel[] = [
  {
    key: 'claude-haiku-4-5',
    tagline: "Anthropic's fastest model — great for high-throughput tasks.",
    capabilities: ['Extended thinking', 'Vision', 'Prompt caching'],
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
      modelName: 'Claude Haiku 4.5',
      maxOutputTokens: 64_000,
      inputPricePerMillionTokens: 1.0,
      outputPricePerMillionTokens: 5.0,
      cacheWritePricePerMillionTokens: 1.25,
      cacheReadPricePerMillionTokens: 0.1,
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
  {
    key: 'claude-sonnet-4-6',
    tagline: 'Balanced reasoning model — Anthropic\'s default workhorse.',
    capabilities: ['Extended thinking', 'Vision', 'Prompt caching'],
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-sonnet-4-6',
      modelName: 'Claude Sonnet 4.6',
      maxOutputTokens: 64_000,
      inputPricePerMillionTokens: 3.0,
      outputPricePerMillionTokens: 15.0,
      cacheWritePricePerMillionTokens: 3.75,
      cacheReadPricePerMillionTokens: 0.3,
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
    key: 'claude-opus-4-7',
    tagline: 'Anthropic\'s most capable model — for the hardest reasoning.',
    capabilities: ['Adaptive thinking', 'Effort control', 'Vision', 'Prompt caching'],
    template: {
      ...claude4xDefaults(),
      modelId: 'us.anthropic.claude-opus-4-7',
      modelName: 'Claude Opus 4.7',
      maxOutputTokens: 64_000,
      inputPricePerMillionTokens: 5.0,
      outputPricePerMillionTokens: 25.0,
      cacheWritePricePerMillionTokens: 6.25,
      cacheReadPricePerMillionTokens: 0.5,
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
];

/**
 * Shared defaults for Bedrock Mantle (OpenAI-compatible open-weight) models.
 *
 * Caching is intentionally absent: prompt caching on Bedrock is model-bound
 * to Anthropic Claude + a small set of Amazon Nova models, none of which run
 * through the Mantle provider, so these never cache and carry no cache
 * pricing. `mantleEndpointPath` is the one Mantle-specific field — sourced
 * from each model card (there is no API that exposes it).
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
      mantleEndpointPath: '/v1',
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
  {
    key: 'gemma-4-31b',
    tagline:
      "Google Gemma 4 31B — reasoning, vision + tool use, served on Mantle's /openai/v1 path.",
    capabilities: ['Reasoning', 'Tool use', 'Vision', '256K context'],
    template: {
      ...mantleDefaults(),
      modelId: 'google.gemma-4-31b',
      modelName: 'Gemma 4 31B',
      providerName: 'Google',
      // Per the AWS model card: text + image + video in, text out. (The
      // request payload note explicitly covers images and video.)
      inputModalities: ['TEXT', 'IMAGE', 'VIDEO'],
      maxInputTokens: 256_000,
      maxOutputTokens: 8_192,
      // Gemma 4 is served on the /openai/v1 path, NOT the default /v1.
      mantleEndpointPath: '/openai/v1',
      inputPricePerMillionTokens: 0.14,
      outputPricePerMillionTokens: 0.4,
      supportedParams: {
        params: {
          temperature: { supported: true, min: 0, max: 2, default: 0.7 },
          top_p: { supported: true, min: 0, max: 1, default: null },
          max_tokens: { supported: true, min: 1, max: 8_192, default: 4_096 },
        },
      },
    },
  },
];

/**
 * Provider-keyed lookup for the catalog tabs. Bedrock + Mantle are populated;
 * OpenAI/Gemini are intentional empty arrays — the page renders a
 * 'Coming soon' empty state when the active tab has no entries.
 */
export const CURATED_MODELS_BY_PROVIDER: Record<ModelProvider, CuratedModel[]> = {
  bedrock: CURATED_BEDROCK_MODELS,
  openai: [],
  gemini: [],
  mantle: CURATED_MANTLE_MODELS,
};
