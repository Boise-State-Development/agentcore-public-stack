/**
 * Available model providers.
 *
 * Two of them are OpenAI-compatible Bedrock surfaces — the backend reaches
 * both over the OpenAI wire protocol with a bearer token rather than the
 * Converse API, which is why neither is just `bedrock`:
 *
 * - `mantle` — Amazon Bedrock Mantle, AWS's OpenAI-compatible surface for
 *   Bedrock-hosted open-weight models.
 * - `bedrock-responses` — the OpenAI **Responses** API on `bedrock-runtime`.
 *   Exists because GPT-5.6 serves prompt caching only over the Responses API;
 *   the same model routed over Converse gets no caching at all.
 */
export type ModelProvider = 'bedrock' | 'openai' | 'gemini' | 'mantle' | 'bedrock-responses';

/**
 * Available model providers as a constant array.
 */
export const AVAILABLE_PROVIDERS: ModelProvider[] = [
  'bedrock',
  'openai',
  'gemini',
  'mantle',
  'bedrock-responses',
];

/**
 * Providers that ride the OpenAI wire protocol against a Bedrock endpoint.
 *
 * These share the `apiMode` / `region` fields and the bearer-token transport.
 * Kept as one list so the form doesn't accumulate `=== 'mantle' || === ...`
 * checks that drift apart.
 */
export const OPENAI_SURFACE_PROVIDERS: readonly ModelProvider[] = ['mantle', 'bedrock-responses'];

/**
 * Providers whose models prompt-cache by default.
 *
 * Mirrors `_resolve_supports_caching` on the backend. The form has to know
 * this because it always posts a `supportsCaching` value — so the backend's
 * own provider default never sees `None` from the UI and can never apply.
 */
export const CACHING_DEFAULT_PROVIDERS: readonly ModelProvider[] = ['bedrock', 'bedrock-responses'];

/**
 * Providers where prompt caching is **not optional**.
 *
 * GPT-5.6 on `bedrock-runtime` caches implicitly, server-side, and we have no
 * way to turn that off. So "supports caching = false" there is simply untrue,
 * and the only thing it changes is that the cache rate fields get cleared —
 * which prices cached tokens at $0.00 while AWS bills them in full. On a warm
 * conversation nearly all input tokens sit in the cache buckets, so that is
 * close to total under-reporting.
 *
 * Treated the same way as `apiMode` on this transport: normalized, not honored.
 */
export const CACHING_FORCED_PROVIDERS: readonly ModelProvider[] = ['bedrock-responses'];

/**
 * Providers whose models *can* prompt-cache, and therefore need the caching
 * controls and cache-rate fields on the admin form.
 *
 * Wider than {@link CACHING_DEFAULT_PROVIDERS} on purpose. `mantle` is not a
 * caching default — most Mantle models are open-weight and don't cache — but
 * `openai.gpt-5.x` there *does*, implicitly, with a cache-read discount and no
 * write fee. Excluding Mantle from the form meant an admin had no way to enable
 * caching or enter a cache-read rate for those models, so their cached tokens
 * were priced at $0.00 while the provider billed them. Measured live on
 * `openai.gpt-5.4`: a warm turn read 3,642 tokens from cache and contributed
 * nothing to the recorded cost.
 */
export const CACHING_CAPABLE_PROVIDERS: readonly ModelProvider[] = [
  'bedrock',
  'bedrock-responses',
  'mantle',
];

/**
 * The caching default for a provider when nothing has been chosen.
 *
 * Mirrors `_resolve_supports_caching` on the backend — kept in step by
 * `tests/shared/test_caching_provider_contract.py`, which reads this file.
 */
export function defaultSupportsCaching(provider: ModelProvider): boolean {
  return CACHING_DEFAULT_PROVIDERS.includes(provider);
}

/**
 * The `supportsCaching` value to persist for a provider.
 *
 * Forced on where caching is unconditional; otherwise the admin's choice wins,
 * falling back to the provider default when unset.
 */
export function supportsCachingForProvider(
  provider: ModelProvider,
  chosen: boolean | null | undefined,
): boolean {
  if (CACHING_FORCED_PROVIDERS.includes(provider)) return true;
  return chosen ?? defaultSupportsCaching(provider);
}

/**
 * Human-readable labels for the provider picker.
 *
 * The raw values are wire identifiers; `bedrock-responses` in particular says
 * nothing useful to an admin choosing a transport.
 */
export const PROVIDER_LABELS: Record<ModelProvider, string> = {
  bedrock: 'Bedrock (Converse)',
  openai: 'OpenAI',
  gemini: 'Google Gemini',
  mantle: 'Bedrock Mantle',
  'bedrock-responses': 'Bedrock Runtime (Responses API)',
};

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
  /**
   * Maximum number of output tokens the model can generate. Optional — newer
   * reasoning/Responses-API models don't publish a discrete output cap. Acts
   * only as a ceiling for the configured max_tokens inference param.
   */
  maxOutputTokens: number | null;
  /** Lifecycle status of the model (e.g., 'ACTIVE', 'LEGACY') */
  modelLifecycle?: string | null;
  /**
   * AppRole IDs that grant this model DIRECTLY. Derived server-side from the
   * role records (the source of truth); saving the form writes it back through
   * to each role's grantedModels.
   */
  allowedAppRoles: string[];
  /**
   * AppRole IDs that grant this model indirectly — via a wildcard ('*') grant or
   * inheritance from a parent role. Read-only: change these by editing the role.
   */
  inheritedAppRoles?: string[];
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
   * OpenAI-compatible API surface: `chat` (OpenAI Chat Completions, the
   * default) or `responses` (OpenAI Responses API — required by models that
   * don't serve Chat Completions, e.g. openai.gpt-5.x).
   *
   * Selectable for `provider === 'mantle'`. Forced to `responses` for
   * `provider === 'bedrock-responses'`, whose whole reason to exist is that
   * GPT-5.6 caches only over that API. Null/absent for every other provider.
   */
  apiMode?: MantleApiMode | null;
  /**
   * Region override for an OpenAI-compatible Bedrock surface (`mantle` or
   * `bedrock-responses`): pins inference to the region hosting the model
   * (e.g. `us-east-1`), independent of where the app runs, and signs the
   * bearer token for it. Null/absent -> the app's region and for other providers.
   */
  region?: string | null;
  /** @deprecated No longer used — the SDK derives the base path from the model id. */
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
  /**
   * Maximum number of output tokens the model can generate. Optional — leave
   * blank when the provider doesn't publish an output cap. Ceiling only for
   * the configured max_tokens inference param; never sent to the provider.
   */
  maxOutputTokens: number | null;
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
   * OpenAI-compatible API surface: `chat` or `responses`. Selectable for
   * `mantle`; forced to `responses` for `bedrock-responses`. Inert for other
   * providers.
   */
  apiMode?: MantleApiMode | null;
  /**
   * Region override for an OpenAI-compatible Bedrock surface (`mantle` or
   * `bedrock-responses`). Empty -> the app's region. Inert for other providers.
   */
  region?: string | null;
  /** Per-model inference parameter capabilities */
  supportedParams?: SupportedParams | null;
}

/** Selectable OpenAI-compatible API surfaces for the model form. */
export const MANTLE_API_MODES = ['chat', 'responses'] as const;
export type MantleApiMode = (typeof MANTLE_API_MODES)[number];

/** Human-readable labels for the OpenAI API surface options. */
export const MANTLE_API_MODE_LABELS: Record<MantleApiMode, string> = {
  chat: 'Chat Completions',
  responses: 'Responses API',
};

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
      'bedrock-responses': { min: 0, max: 2 },
    },
    providers: ['bedrock', 'openai', 'gemini', 'mantle', 'bedrock-responses'],
  },
  {
    key: 'top_p',
    label: 'Top P',
    description: 'Nucleus sampling cutoff.',
    kind: 'number',
    defaultMin: 0,
    defaultMax: 1,
    providers: ['bedrock', 'openai', 'gemini', 'mantle', 'bedrock-responses'],
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
    providers: ['bedrock', 'openai', 'gemini', 'mantle', 'bedrock-responses'],
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
    description:
      'Reasoning depth (OpenAI o-series and reasoning models on the ' +
      'OpenAI-compatible Bedrock surfaces).',
    kind: 'number',
    providers: ['openai', 'mantle', 'bedrock-responses'],
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
