/**
 * Summary information for a Bedrock foundation model.
 * Matches the FoundationModelSummary model from the Python API.
 */
export interface FoundationModelSummary {
  /** Unique identifier for the model */
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
}

/**
 * Response model for listing Bedrock foundation models.
 * Matches the BedrockModelsResponse model from the Python API.
 */
export interface BedrockModelsResponse {
  /** List of foundation model summaries */
  models: FoundationModelSummary[];
  /** Pagination token for next page (not supported by Bedrock API) */
  nextToken: string | null;
  /** Total count of models returned */
  totalCount?: number | null;
}

/**
 * Query parameters for listing Bedrock models.
 */
export interface ListBedrockModelsParams {
  /** Filter by provider name (e.g., 'Anthropic', 'Amazon') */
  byProvider?: string;
  /** Filter by output modality (e.g., 'TEXT', 'IMAGE') */
  byOutputModality?: string;
  /** Filter by inference type (e.g., 'ON_DEMAND', 'PROVISIONED') */
  byInferenceType?: string;
  /** Filter by customization type (e.g., 'FINE_TUNING', 'CONTINUED_PRE_TRAINING') */
  byCustomizationType?: string;
  /** Maximum number of models to return (client-side limit) */
  maxResults?: number;
}
