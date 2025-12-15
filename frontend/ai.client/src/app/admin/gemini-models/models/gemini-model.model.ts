/**
 * Summary information for a Gemini model.
 * Matches the GeminiModelSummary model from the Python API.
 */
export interface GeminiModelSummary {
  /** Model name (e.g., 'gemini-2.0-flash-exp', 'gemini-1.5-pro') */
  name: string;
  /** Base model identifier */
  baseModelId?: string;
  /** Version of the model */
  version?: string;
  /** Display name for the model */
  displayName: string;
  /** Model description */
  description?: string;
  /** Maximum input tokens */
  inputTokenLimit?: number;
  /** Maximum output tokens */
  outputTokenLimit?: number;
  /** List of supported generation methods (e.g., 'generateContent', 'streamGenerateContent') */
  supportedGenerationMethods: string[];
  /** Whether this is a thinking/reasoning model */
  thinking?: boolean;
  /** Default temperature */
  temperature?: number;
  /** Maximum temperature */
  maxTemperature?: number;
  /** Top-p range */
  topP?: number;
  /** Top-k range */
  topK?: number;
}

/**
 * Response model for listing Gemini models.
 * Matches the GeminiModelsResponse model from the Python API.
 */
export interface GeminiModelsResponse {
  /** List of Gemini model summaries */
  models: GeminiModelSummary[];
  /** Total count of models returned */
  totalCount: number;
}

/**
 * Query parameters for listing Gemini models.
 */
export interface ListGeminiModelsParams {
  /** Maximum number of models to return (client-side limit) */
  maxResults?: number;
}
