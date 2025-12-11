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
  /** Provider name (e.g., 'Anthropic', 'Amazon', 'Meta') */
  providerName: string;
  /** List of supported input modalities (e.g., 'TEXT', 'IMAGE') */
  inputModalities: string[];
  /** List of supported output modalities (e.g., 'TEXT', 'IMAGE') */
  outputModalities: string[];
  /** Whether the model supports response streaming */
  responseStreamingSupported: boolean;
  /** List of customization types supported (e.g., 'FINE_TUNING') */
  customizationsSupported: string[];
  /** List of inference types supported (e.g., 'ON_DEMAND', 'PROVISIONED') */
  inferenceTypesSupported: string[];
  /** Lifecycle status of the model (e.g., 'ACTIVE', 'LEGACY') */
  modelLifecycle?: string | null;
  /** Roles that have access to this model */
  availableToRoles: string[];
  /** Whether the model is enabled for use */
  enabled: boolean;
  /** Input price per million tokens (in USD) */
  inputPricePerMillionTokens: number;
  /** Output price per million tokens (in USD) */
  outputPricePerMillionTokens: number;
  /** Date the model was added to the system */
  createdAt?: Date;
  /** Date the model was last updated */
  updatedAt?: Date;
}

/**
 * Form data for creating or editing a managed model.
 */
export interface ManagedModelFormData {
  /** Bedrock model ID */
  modelId: string;
  /** Human-readable name of the model */
  modelName: string;
  /** Provider name (e.g., 'Anthropic', 'Amazon', 'Meta') */
  providerName: string;
  /** List of supported input modalities */
  inputModalities: string[];
  /** List of supported output modalities */
  outputModalities: string[];
  /** Whether the model supports response streaming */
  responseStreamingSupported: boolean;
  /** List of customization types supported */
  customizationsSupported: string[];
  /** List of inference types supported */
  inferenceTypesSupported: string[];
  /** Lifecycle status of the model */
  modelLifecycle?: string | null;
  /** Roles that have access to this model */
  availableToRoles: string[];
  /** Whether the model is enabled for use */
  enabled: boolean;
  /** Input price per million tokens (in USD) */
  inputPricePerMillionTokens: number;
  /** Output price per million tokens (in USD) */
  outputPricePerMillionTokens: number;
}

/**
 * Available roles that can be assigned to models.
 */
export const AVAILABLE_ROLES = [
  'Admin',
  'SuperAdmin',
  'DotNetDevelopers',
  'User',
  'Guest',
] as const;
