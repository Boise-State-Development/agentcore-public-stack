/**
 * Tool category enum
 */
export type ToolCategory =
  | 'search'
  | 'data'
  | 'visualization'
  | 'document'
  | 'code'
  | 'browser'
  | 'utility'
  | 'research'
  | 'finance'
  | 'gateway'
  | 'custom';

/**
 * Tool protocol enum
 */
export type ToolProtocol = 'local' | 'aws_sdk' | 'mcp' | 'mcp_external' | 'a2a';

/**
 * MCP transport type
 */
export type MCPTransport = 'streamable-http' | 'sse' | 'stdio';

/**
 * MCP authentication type
 */
export type MCPAuthType = 'none' | 'aws-iam' | 'api-key' | 'bearer-token' | 'oauth2';

/**
 * A2A authentication type
 */
export type A2AAuthType = 'none' | 'aws-iam' | 'agentcore' | 'api-key';

/**
 * Tool status enum
 */
export type ToolStatus = 'active' | 'deprecated' | 'disabled' | 'coming_soon';

/**
 * MCP server configuration for external MCP tools.
 */
export interface MCPServerConfig {
  serverUrl: string;
  transport: MCPTransport;
  authType: MCPAuthType;
  awsRegion?: string | null;
  apiKeyHeader?: string | null;
  secretArn?: string | null;
  tools: string[];
  healthCheckEnabled: boolean;
  healthCheckIntervalSeconds: number;
}

/**
 * A2A agent configuration for agent-to-agent tools.
 */
export interface A2AAgentConfig {
  agentUrl: string;
  agentId?: string | null;
  authType: A2AAuthType;
  awsRegion?: string | null;
  secretArn?: string | null;
  capabilities: string[];
  timeoutSeconds: number;
  maxRetries: number;
}

/**
 * Admin tool definition with role assignments.
 */
export interface AdminTool {
  toolId: string;
  displayName: string;
  description: string;
  category: ToolCategory;
  icon: string | null;
  protocol: ToolProtocol;
  status: ToolStatus;
  requiresOauthProvider: string | null;
  isPublic: boolean;
  allowedAppRoles: string[];
  enabledByDefault: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string | null;
  updatedBy: string | null;
  // External tool configurations
  mcpConfig?: MCPServerConfig | null;
  a2aConfig?: A2AAgentConfig | null;
}

/**
 * Response for listing admin tools.
 */
export interface AdminToolListResponse {
  tools: AdminTool[];
  total: number;
}

/**
 * Role assignment for a tool.
 */
export interface ToolRoleAssignment {
  roleId: string;
  displayName: string;
  grantType: 'direct' | 'inherited';
  inheritedFrom: string | null;
  enabled: boolean;
}

/**
 * Response for getting tool roles.
 */
export interface ToolRolesResponse {
  toolId: string;
  roles: ToolRoleAssignment[];
}

/**
 * Request for creating a new tool.
 */
export interface ToolCreateRequest {
  toolId: string;
  displayName: string;
  description: string;
  category?: ToolCategory;
  icon?: string;
  protocol?: ToolProtocol;
  status?: ToolStatus;
  requiresOauthProvider?: string | null;
  isPublic?: boolean;
  enabledByDefault?: boolean;
  mcpConfig?: MCPServerConfig;
  a2aConfig?: A2AAgentConfig;
}

/**
 * Request for updating a tool.
 */
export interface ToolUpdateRequest {
  displayName?: string;
  description?: string;
  category?: ToolCategory;
  icon?: string;
  protocol?: ToolProtocol;
  status?: ToolStatus;
  requiresOauthProvider?: string | null;
  isPublic?: boolean;
  enabledByDefault?: boolean;
  mcpConfig?: MCPServerConfig | null;
  a2aConfig?: A2AAgentConfig | null;
}

/**
 * Request for setting tool roles.
 */
export interface SetToolRolesRequest {
  appRoleIds: string[];
}

/**
 * Result of syncing tool catalog from registry.
 */
export interface SyncResult {
  discovered: { tool_id: string; display_name: string; action: string }[];
  orphaned: { tool_id: string; action: string }[];
  unchanged: string[];
  dryRun: boolean;
}

/**
 * Form data model for creating/editing a tool.
 */
export interface ToolFormData {
  toolId: string;
  displayName: string;
  description: string;
  category: ToolCategory;
  icon: string;
  protocol: ToolProtocol;
  status: ToolStatus;
  requiresOauthProvider: string | null;
  isPublic: boolean;
  enabledByDefault: boolean;
  // MCP configuration (for mcp_external protocol)
  mcpServerUrl?: string;
  mcpTransport?: MCPTransport;
  mcpAuthType?: MCPAuthType;
  mcpAwsRegion?: string;
  mcpApiKeyHeader?: string;
  mcpSecretArn?: string;
  mcpTools?: string;  // Comma-separated list
  mcpHealthCheckEnabled?: boolean;
  // A2A configuration (for a2a protocol)
  a2aAgentUrl?: string;
  a2aAgentId?: string;
  a2aAuthType?: A2AAuthType;
  a2aAwsRegion?: string;
  a2aSecretArn?: string;
  a2aCapabilities?: string;  // Comma-separated list
  a2aTimeoutSeconds?: number;
  a2aMaxRetries?: number;
}

/**
 * Available tool categories for dropdowns.
 */
export const TOOL_CATEGORIES: { value: ToolCategory; label: string }[] = [
  { value: 'search', label: 'Search' },
  { value: 'data', label: 'Data' },
  { value: 'visualization', label: 'Visualization' },
  { value: 'document', label: 'Document' },
  { value: 'code', label: 'Code' },
  { value: 'browser', label: 'Browser' },
  { value: 'utility', label: 'Utility' },
  { value: 'research', label: 'Research' },
  { value: 'finance', label: 'Finance' },
  { value: 'gateway', label: 'Gateway' },
  { value: 'custom', label: 'Custom' },
];

/**
 * Available tool protocols for dropdowns.
 */
export const TOOL_PROTOCOLS: { value: ToolProtocol; label: string; description?: string }[] = [
  { value: 'local', label: 'Local (Direct Function)', description: 'Tool implemented as a local function in the codebase' },
  { value: 'aws_sdk', label: 'AWS SDK (Bedrock)', description: 'AWS Bedrock built-in tools (Code Interpreter, Browser)' },
  { value: 'mcp', label: 'MCP Gateway (AgentCore)', description: 'MCP tools via AgentCore Gateway' },
  { value: 'mcp_external', label: 'MCP External Server', description: 'Connect to an externally deployed MCP server' },
  { value: 'a2a', label: 'Agent-to-Agent', description: 'Delegate tasks to another AI agent' },
];

/**
 * Available MCP transport types for dropdowns.
 */
export const MCP_TRANSPORTS: { value: MCPTransport; label: string }[] = [
  { value: 'streamable-http', label: 'Streamable HTTP' },
  { value: 'sse', label: 'Server-Sent Events (SSE)' },
  { value: 'stdio', label: 'Standard I/O (Local Only)' },
];

/**
 * Available MCP authentication types for dropdowns.
 */
export const MCP_AUTH_TYPES: { value: MCPAuthType; label: string; description?: string }[] = [
  { value: 'none', label: 'None', description: 'No authentication required' },
  { value: 'aws-iam', label: 'AWS IAM (SigV4)', description: 'AWS IAM authentication with SigV4 signing' },
  { value: 'api-key', label: 'API Key', description: 'API key in request header' },
  { value: 'bearer-token', label: 'Bearer Token', description: 'Bearer token authentication' },
  { value: 'oauth2', label: 'OAuth 2.0', description: 'OAuth 2.0 client credentials flow' },
];

/**
 * Available A2A authentication types for dropdowns.
 */
export const A2A_AUTH_TYPES: { value: A2AAuthType; label: string; description?: string }[] = [
  { value: 'none', label: 'None', description: 'No authentication required' },
  { value: 'aws-iam', label: 'AWS IAM (SigV4)', description: 'AWS IAM authentication with SigV4 signing' },
  { value: 'agentcore', label: 'AgentCore Runtime', description: 'AgentCore Runtime authentication' },
  { value: 'api-key', label: 'API Key', description: 'API key in request header' },
];

/**
 * Available tool statuses for dropdowns.
 */
export const TOOL_STATUSES: { value: ToolStatus; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'deprecated', label: 'Deprecated' },
  { value: 'disabled', label: 'Disabled' },
  { value: 'coming_soon', label: 'Coming Soon' },
];
