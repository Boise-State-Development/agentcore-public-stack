/**
 * Available model providers.
 *
 * `mantle` is Amazon Bedrock Mantle — AWS's OpenAI-compatible inference
 * surface for Bedrock-hosted open-weight models. It is a distinct provider
 * from `bedrock` because the backend reaches it over the OpenAI wire
 * protocol with a bearer token rather than the Converse API.
 */
export type ModelProvider = 'bedrock' | 'openai' | 'gemini' | 'mantle';

/**
 * Available model providers as a constant array.
 */
export const AVAILABLE_PROVIDERS: ModelProvider[] = ['bedrock', 'openai', 'gemini', 'mantle'];

/**
 * Capability + bounds for a single inference parameter.
 *
 * Drives the admin form (which knobs are exposed, what bounds to enforce)
 * and the runtime gate on the backend (whether to send the param to the
 * provider SDK at all). `default` is what gets sent when the user doesn't
 * override; `locked` reserves the slot for the future user-tweak surface.
 */
export interface ModelParamSpec {
  supported: boolean;
  min?: number | null;
  max?: number | null;
  /**
   * Permissible values for enum-style params (e.g. `effort`). When set,
   * `default` and any user override must be a member; `min`/`max` don't
   * apply. The per-model difference (Sonnet 4.6 vs Opus 4.7 effort tiers)
   * lives here as data — no model-family branching in code.
   */
  allowed?: (string | number)[] | null;
  default?: number | boolean | string | null;
  locked?: boolean;
}

/**
 * Per-model inference parameter capability map, keyed by canonical name
 * (e.g. `temperature`, `top_p`, `top_k`, `max_tokens`, `thinking`,
 * `reasoning_effort`).
 *
 * Open-ended on purpose: each provider's translation table on the backend
 * decides which canonical names map to native SDK fields, and silently
 * drops the rest. Adding a new well-known param is a frontend catalog
 * entry plus one backend mapping line — not a schema migration.
 */
export interface SupportedParams {
  params: Record<string, ModelParamSpec>;
}

/**
 * Represents a managed model in the system.
 * This extends the Bedrock foundation model with additional metadata
 * for role-based access control and pricing.
 */
export interface ManagedModel {
  /** Unique identifier for the model */
  id: string;
  /** Bedrock model ID */
  modelId: string;
  /** Human-readable name of the model */
  modelName: string;
  /** Model provider (AWS, OpenAI, Google) */
  provider: ModelProvider;
  /** Provider name (e.g., 'Anthropic', 'Amazon', 'Meta') */
  providerName: string;
  /** List of supported input modalities (e.g., 'TEXT', 'IMAGE') */
  inputModalities: string[];
  /** List of supported output modalities (e.g., 'TEXT', 'IMAGE') */
  outputModalities: string[];
  /** Whether the model supports response streaming */
  responseStreamingSupported?: boolean;
  /** Maximum number of input tokens the model can accept */
  maxInputTokens: number;
  /** Maximum number of output tokens the model can generate */
  maxOutputTokens: number;
  /** Lifecycle status of the model (e.g., 'ACTIVE', 'LEGACY') */
  modelLifecycle?: string | null;
  /** AppRole IDs that have access to this model (preferred over availableToRoles) */
  allowedAppRoles: string[];
  /** @deprecated Legacy JWT role names - use allowedAppRoles instead */
  availableToRoles: string[];
  /** Whether the model is enabled for use */
  enabled: boolean;
  /** Input price per million tokens (in USD) */
  inputPricePerMillionTokens: number;
  /** Output price per million tokens (in USD) */
  outputPricePerMillionTokens: number;
  /** Cache write price per million tokens (in USD) - Bedrock only */
  cacheWritePricePerMillionTokens?: number | null;
  /** Cache read price per million tokens (in USD) - Bedrock only */
  cacheReadPricePerMillionTokens?: number | null;
  /** Knowledge cutoff date for the model */
  knowledgeCutoffDate?: string | null;
  /** Whether this model supports prompt caching (Bedrock only) */
  supportsCaching: boolean;
  /** Whether this is the default model for new sessions */
  isDefault: boolean;
  /**
   * Bedrock Mantle endpoint path (`provider === 'mantle'` only): `/v1`
   * (OpenAI Chat Completions, the default) or `/openai/v1` (e.g. Gemma 4).
   * Sourced from the model card — there is no API that exposes it. Null/absent
   * for every other provider.
   */
  mantleEndpointPath?: string | null;
  /** Per-model inference parameter capabilities (temperature, top_p, etc.) */
  supportedParams?: SupportedParams | null;
  /** Date the model was added to the system (ISO string from API) */
  createdAt?: string | Date;
  /** Date the model was last updated (ISO string from API) */
  updatedAt?: string | Date;
}

/**
 * Form data for creating or editing a managed model.
 */
export interface ManagedModelFormData {
  /** Bedrock model ID */
  modelId: string;
  /** Human-readable name of the model */
  modelName: string;
  /** Model provider (AWS, OpenAI, Google) */
  provider: ModelProvider;
  /** Provider name (e.g., 'Anthropic', 'Amazon', 'Meta') */
  providerName: string;
  /** List of supported input modalities */
  inputModalities: string[];
  /** List of supported output modalities */
  outputModalities: string[];
  /** Whether the model supports response streaming */
  responseStreamingSupported: boolean;
  /** Maximum number of input tokens the model can accept */
  maxInputTokens: number;
  /** Maximum number of output tokens the model can generate */
  maxOutputTokens: number;
  /** Lifecycle status of the model */
  modelLifecycle?: string | null;
  /** AppRole IDs that have access to this model */
  allowedAppRoles: string[];
  /** @deprecated Legacy JWT role names - use allowedAppRoles instead */
  availableToRoles: string[];
  /** Whether the model is enabled for use */
  enabled: boolean;
  /** Input price per million tokens (in USD) */
  inputPricePerMillionTokens: number;
  /** Output price per million tokens (in USD) */
  outputPricePerMillionTokens: number;
  /** Cache write price per million tokens (in USD) - Bedrock only */
  cacheWritePricePerMillionTokens?: number | null;
  /** Cache read price per million tokens (in USD) - Bedrock only */
  cacheReadPricePerMillionTokens?: number | null;
  /** Knowledge cutoff date for the model */
  knowledgeCutoffDate?: string | null;
  /** Whether this model supports prompt caching (Bedrock only) */
  supportsCaching?: boolean;
  /** Whether this is the default model for new sessions */
  isDefault: boolean;
  /**
   * Bedrock Mantle endpoint path (`provider === 'mantle'` only): `/v1` or
   * `/openai/v1`. Inert for other providers.
   */
  mantleEndpointPath?: string | null;
  /** Per-model inference parameter capabilities */
  supportedParams?: SupportedParams | null;
}

/** Selectable Bedrock Mantle endpoint paths for the model form. */
export const MANTLE_ENDPOINT_PATHS = ['/v1', '/openai/v1'] as const;
export type MantleEndpointPath = (typeof MANTLE_ENDPOINT_PATHS)[number];

/**
 * Frontend catalog of well-known canonical inference params.
 *
 * Drives the admin form's per-param row: friendly label, input widget, and
 * suggested bounds. The backend does the actual provider translation via
 * its own table — names here just need to match what's in the backend's
 * `_<PROVIDER>_PARAM_MAP`. Add a new param here + on the backend; no
 * schema migration required.
 */
export interface ParamBoundsDefaults {
  min?: number;
  max?: number;
}

export interface KnownParamMeta {
  key: string;
  label: string;
  description: string;
  /**
   * `thinkingBudget` is a number input gated by an on/off switch. The
   * stored value is `null` (off) or an int budget (on). The runtime
   * translator wraps the int into the provider-native shape.
   */
  kind: 'number' | 'integer' | 'toggle' | 'thinkingBudget' | 'select';
  /**
   * Universe of selectable values for `kind: 'select'`. The admin checks the
   * subset this model supports (stored as `ModelParamSpec.allowed`); the
   * default is chosen from that subset. Ordered low->high.
   */
  options?: string[];
  /** Catalog-wide fallback range, used when no provider-specific entry applies. */
  defaultMin?: number;
  defaultMax?: number;
  /**
   * Per-provider seeded bounds. Wins over `defaultMin`/`defaultMax` when the
   * model's selected provider has an entry. Lets us serve the right range
   * out of the box (e.g. temperature 0–1 on Bedrock vs 0–2 on OpenAI) without
   * making the admin look up SDK docs.
   */
  defaults?: Partial<Record<ModelProvider, ParamBoundsDefaults>>;
  /** Providers that translate this canonical name. Used to filter the form. */
  providers: ModelProvider[];
  /**
   * Other canonical params that must be suppressed when this one is enabled
   * (truthy). Used by the form/runtime to silently drop conflicting values
   * — e.g. Anthropic rejects `temperature`/`top_p`/`top_k` while extended
   * thinking is on.
   */
  incompatibleWith?: string[];
}

export const KNOWN_PARAMS: KnownParamMeta[] = [
  {
    key: 'temperature',
    label: 'Temperature',
    description: 'Sampling randomness. Lower = more deterministic.',
    kind: 'number',
    defaults: {
      bedrock: { min: 0, max: 1 },   // Anthropic/Bedrock cap
      openai: { min: 0, max: 2 },    // OpenAI accepts 0–2
      gemini: { min: 0, max: 1 },
      mantle: { min: 0, max: 2 },    // OpenAI wire protocol range
    },
    providers: ['bedrock', 'openai', 'gemini', 'mantle'],
  },
  {
    key: 'top_p',
    label: 'Top P',
    description: 'Nucleus sampling cutoff.',
    kind: 'number',
    defaultMin: 0,
    defaultMax: 1,
    providers: ['bedrock', 'openai', 'gemini', 'mantle'],
  },
  {
    key: 'top_k',
    label: 'Top K',
    description: 'Top-k sampling cutoff. Not supported by OpenAI.',
    kind: 'integer',
    defaultMin: 1,
    providers: ['bedrock', 'gemini'],
  },
  {
    key: 'max_tokens',
    label: 'Max Output Tokens',
    description: 'Maximum tokens in the model response.',
    kind: 'integer',
    defaultMin: 1,
    providers: ['bedrock', 'openai', 'gemini', 'mantle'],
  },
  {
    key: 'thinking',
    label: 'Extended Thinking',
    description:
      'Token budget for extended reasoning. Must be ≥ 1024 and < max_tokens. ' +
      'Disables temperature, top_p, top_k while on (Anthropic constraint).',
    kind: 'thinkingBudget',
    defaultMin: 1024,
    providers: ['bedrock', 'gemini'],
    incompatibleWith: ['temperature', 'top_p', 'top_k'],
  },
  {
    key: 'effort',
    label: 'Effort',
    description:
      'Reasoning/output effort (Anthropic output_config.effort). Higher = ' +
      'more thorough, more tokens. On adaptive-thinking models it governs ' +
      'thinking depth. Check the levels this model supports; pick a default.',
    kind: 'select',
    options: ['low', 'medium', 'high', 'xhigh', 'max'],
    providers: ['bedrock'],
  },
  {
    key: 'reasoning_effort',
    label: 'Reasoning Effort',
    description: 'Reasoning depth (OpenAI o-series and reasoning models on Bedrock Mantle).',
    kind: 'number',
    providers: ['openai', 'mantle'],
  },
];

/**
 * @deprecated Use AppRoles from the /admin/roles API instead.
 * These legacy JWT roles are kept for backward compatibility only.
 */
export const AVAILABLE_ROLES = [
  'Admin',
  'SuperAdmin',
  'DotNetDevelopers',
  'User',
  'Guest',
] as const;
