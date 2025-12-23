/**
 * Memory data models for AgentCore Memory
 */

/**
 * A single memory record from AgentCore Memory
 */
export interface MemoryRecord {
  recordId?: string;
  content: string;
  namespace?: string;
  relevanceScore?: number;
  createdAt?: string;
  updatedAt?: string;
  metadata?: Record<string, unknown>;
}

/**
 * Response containing a list of memory records
 */
export interface MemoriesResponse {
  memories: MemoryRecord[];
  namespace: string;
  query?: string;
  totalCount: number;
}

/**
 * Combined response for all user memories
 */
export interface AllMemoriesResponse {
  preferences: MemoriesResponse;
  facts: MemoriesResponse;
}

/**
 * Memory strategy configuration
 */
export interface MemoryStrategy {
  strategyId: string;
  strategyType: string;
  namespace?: string;
  status?: string;
  config?: Record<string, unknown>;
}

/**
 * Response containing memory strategies
 */
export interface StrategiesResponse {
  strategies: MemoryStrategy[];
  memoryId: string;
}

/**
 * Memory status information
 */
export interface MemoryStatus {
  status: 'available' | 'unavailable';
  available: boolean;
  mode: 'cloud' | 'local' | 'unknown';
  memoryId?: string;
  region?: string;
  namespaces?: {
    preferences: string;
    facts: string;
  };
  error?: string;
}

/**
 * Request for semantic memory search
 */
export interface MemorySearchRequest {
  query: string;
  namespace?: string;
  topK?: number;
}

/**
 * Response after deleting a memory
 */
export interface DeleteMemoryResponse {
  deletedCount: number;
  message: string;
}
