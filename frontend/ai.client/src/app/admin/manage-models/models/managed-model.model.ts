/**
 * Available model providers.
 */
export type ModelProvider = 'bedrock' | 'openai' | 'gemini';

/**
 * Available model providers as a constant array.
 */
export const AVAILABLE_PROVIDERS: ModelProvider[] = ['bedrock', 'openai', 'gemini'];

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
  /** Whether this is a reasoning model (e.g., o1, o3) */
  isReasoningModel: boolean;
  /** Knowledge cutoff date for the model */
  knowledgeCutoffDate?: string | null;
  /** Whether this model supports prompt caching (Bedrock only) */
  supportsCaching: boolean;
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
  /** Whether this is a reasoning model (e.g., o1, o3) */
  isReasoningModel: boolean;
  /** Knowledge cutoff date for the model */
  knowledgeCutoffDate?: string | null;
  /** Whether this model supports prompt caching (Bedrock only) */
  supportsCaching?: boolean;
}

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
