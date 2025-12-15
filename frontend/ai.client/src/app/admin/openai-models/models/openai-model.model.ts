/**
 * Summary information for an OpenAI model.
 * Matches the OpenAIModelSummary model from the Python API.
 *
 * Note: OpenAI's list models endpoint provides limited information.
 * For detailed model specifications, see: https://platform.openai.com/docs/models/compare
 */
export interface OpenAIModelSummary {
  /** Model identifier (e.g., 'gpt-4o', 'gpt-4-turbo', 'gpt-3.5-turbo') */
  id: string;
  /** Unix timestamp when the model was created */
  created?: number;
  /** Organization that owns the model (e.g., 'openai', 'system') */
  ownedBy: string;
  /** Object type, typically 'model' */
  object?: string;
}

/**
 * Response model for listing OpenAI models.
 * Matches the OpenAIModelsResponse model from the Python API.
 */
export interface OpenAIModelsResponse {
  /** List of OpenAI model summaries */
  models: OpenAIModelSummary[];
  /** Total count of models returned */
  totalCount: number;
}

/**
 * Query parameters for listing OpenAI models.
 */
export interface ListOpenAIModelsParams {
  /** Maximum number of models to return (client-side limit) */
  maxResults?: number;
}
