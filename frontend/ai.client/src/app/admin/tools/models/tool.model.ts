/**
 * Tool metadata from the catalog.
 */
export interface Tool {
  /** Unique tool identifier (function name) */
  toolId: string;
  /** Human-readable display name */
  name: string;
  /** Tool description */
  description: string;
  /** Tool category (search, browser, data, utilities, code, gateway) */
  category: ToolCategory;
  /** Whether this is a gateway/MCP tool */
  isGatewayTool: boolean;
  /** API key required for this tool (if any) */
  requiresApiKey: string | null;
  /** Icon name for UI */
  icon: string | null;
}

/**
 * Tool categories for grouping in UI.
 */
export type ToolCategory = 'search' | 'browser' | 'data' | 'utilities' | 'code' | 'gateway';

/**
 * Response model for listing tools.
 */
export interface ToolListResponse {
  tools: Tool[];
  total: number;
}

/**
 * User's tool permissions.
 */
export interface UserToolPermissions {
  userId: string;
  allowedTools: string[];
  hasWildcard: boolean;
  appRoles: string[];
}
