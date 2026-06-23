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
 * AgentCore Gateway target listing mode. DYNAMIC disables 3LO + semantic search.
 */
export type GatewayListingMode = 'default' | 'dynamic';

/**
 * How the Gateway authenticates outbound to a target's MCP endpoint.
 */
export type GatewayCredentialType = 'none' | 'gateway_iam_role' | 'oauth' | 'api_key';

/**
 * OAuth grant the Gateway uses for an OAUTH-credentialed target.
 */
export type GatewayOAuthGrantType =
  | 'authorization_code'
  | 'client_credentials'
  | 'token_exchange';

/**
 * Tool status enum
 */
export type ToolStatus = 'active' | 'deprecated' | 'disabled' | 'coming_soon';

/**
 * A single tool exposed by an MCP server, with per-tool flags.
 */
export interface MCPToolEntry {
  name: string;
  needsApproval: boolean;
  description?: string | null;
}

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
  tools: MCPToolEntry[];
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
 * Gateway target configuration for protocol='mcp' tools. Mirrors the Python
 * MCPGatewayConfig. `targetId`/`gatewayArn` are AWS-assigned (present on
 * responses, omitted on create/update requests).
 */
export interface MCPGatewayConfig {
  targetName: string;
  endpointUrl: string;
  listingMode: GatewayListingMode;
  credentialType: GatewayCredentialType;
  credentialProviderArn?: string | null;
  awsService?: string | null;
  awsRegion?: string | null;
  /**
   * Name (or ARN) of the Lambda backing a GATEWAY_IAM_ROLE Function-URL target.
   * Lets the platform grant the gateway role InvokeFunctionUrl on exactly this
   * function at registration (no infra change). Same-account only.
   */
  lambdaFunctionName?: string | null;
  oauthScopes: string[];
  grantType: GatewayOAuthGrantType;
  customParameters?: Record<string, string> | null;
  tools: MCPToolEntry[];
  targetId?: string | null;
  gatewayArn?: string | null;
}

/**
 * Admin tool definition with role assignments.
 */
export interface AdminTool {
  toolId: string;
  displayName: string;
  description: string;
  category: ToolCategory;
  protocol: ToolProtocol;
  status: ToolStatus;
  requiresOauthProvider: string | null;
  forwardAuthToken: boolean;
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
  mcpGatewayConfig?: MCPGatewayConfig | null;
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
  protocol?: ToolProtocol;
  status?: ToolStatus;
  requiresOauthProvider?: string | null;
  forwardAuthToken?: boolean;
  isPublic?: boolean;
  enabledByDefault?: boolean;
  mcpConfig?: MCPServerConfig;
  a2aConfig?: A2AAgentConfig;
  mcpGatewayConfig?: MCPGatewayConfig;
}

/**
 * Request for updating a tool.
 */
export interface ToolUpdateRequest {
  displayName?: string;
  description?: string;
  category?: ToolCategory;
  protocol?: ToolProtocol;
  status?: ToolStatus;
  requiresOauthProvider?: string | null;
  forwardAuthToken?: boolean;
  isPublic?: boolean;
  enabledByDefault?: boolean;
  mcpConfig?: MCPServerConfig | null;
  a2aConfig?: A2AAgentConfig | null;
  mcpGatewayConfig?: MCPGatewayConfig | null;
}

/**
 * Request for setting tool roles.
 */
export interface SetToolRolesRequest {
  appRoleIds: string[];
}

/**
 * Request body for POST /api/admin/tools/discover.
 */
export interface MCPDiscoverRequest {
  serverUrl: string;
  transport: MCPTransport;
  authType: MCPAuthType;
  awsRegion?: string | null;
  apiKeyHeader?: string | null;
  secretArn?: string | null;
  /**
   * When true, discovery is signed with the admin's own OIDC token (matching
   * the catalog `forwardAuthToken` flag) instead of SigV4 — for same-team MCP
   * servers that validate a forwarded JWT (Lambda Function URL AuthType=NONE).
   */
  forwardAuthToken?: boolean;
}

/**
 * A tool returned by MCP server discovery.
 */
export interface DiscoveredMCPTool {
  name: string;
  description?: string | null;
}

/**
 * Response from POST /api/admin/tools/discover.
 */
export interface MCPDiscoverResponse {
  tools: DiscoveredMCPTool[];
}

/**
 * Live health of the AgentCore Gateway target backing a protocol='mcp' tool,
 * from GET /api/admin/tools/{toolId}/gateway-status. The gateway connects to
 * and lists the target's tools asynchronously after registration, so a tool
 * can be 'active' yet unusable because its target FAILED to sync. `status` is
 * the gateway target status (CREATING / READY / FAILED / UPDATE_UNSUCCESSFUL /
 * MISSING); `statusReasons` explains an unhealthy target.
 */
export interface GatewayTargetStatus {
  targetId: string;
  status: string;
  statusReasons: string[];
  healthy: boolean;
}

/**
 * Form data model for creating/editing a tool.
 */
export interface ToolFormData {
  toolId: string;
  displayName: string;
  description: string;
  category: ToolCategory;
  protocol: ToolProtocol;
  status: ToolStatus;
  requiresOauthProvider: string | null;
  forwardAuthToken: boolean;
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
 * Available Gateway target listing modes for dropdowns.
 */
export const GATEWAY_LISTING_MODES: { value: GatewayListingMode; label: string; description?: string }[] = [
  { value: 'default', label: 'Default', description: 'Static tool listing — required for OAuth (3LO) and semantic search' },
  { value: 'dynamic', label: 'Dynamic', description: 'Resolve tools at call time — disables 3LO and semantic search' },
];

/**
 * Available Gateway outbound credential types for dropdowns.
 */
export const GATEWAY_CREDENTIAL_TYPES: { value: GatewayCredentialType; label: string; description?: string }[] = [
  { value: 'none', label: 'None (public endpoint)', description: 'No outbound credentials — the endpoint is publicly reachable' },
  { value: 'gateway_iam_role', label: 'Gateway IAM Role (SigV4)', description: 'The gateway signs with its execution role — requires the AWS service to sign for' },
  { value: 'oauth', label: 'OAuth (3LO / 2LO)', description: 'Reference an existing OAuth credential provider by ARN' },
  { value: 'api_key', label: 'API Key', description: 'Reference an existing API-key credential provider by ARN' },
];

/**
 * Available Gateway OAuth grant types for dropdowns.
 */
export const GATEWAY_OAUTH_GRANT_TYPES: { value: GatewayOAuthGrantType; label: string; description?: string }[] = [
  { value: 'authorization_code', label: 'Authorization Code (3LO)', description: 'On-behalf-of-user — requires the user to connect the provider' },
  { value: 'client_credentials', label: 'Client Credentials (2LO)', description: 'Machine-to-machine — no user consent' },
  { value: 'token_exchange', label: 'Token Exchange', description: 'Exchange an existing token' },
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

/**
 * Derive the AWS service name for SigV4 signing from a known AWS endpoint host.
 *
 * Mirrors the backend `detect_aws_service_from_url`, but returns `''` for an
 * unrecognised host (so the Gateway form leaves the field for the admin to
 * fill) rather than defaulting to `'lambda'` — the backend's last-resort
 * default is only appropriate at signing time.
 */
export function detectAwsServiceFromUrl(url: string): string {
  if (/\.lambda-url\.[a-z0-9-]+\.on\.aws/.test(url)) return 'lambda';
  if (/\.execute-api\.[a-z0-9-]+\.amazonaws\.com/.test(url)) return 'execute-api';
  if (/\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com/.test(url)) return 'bedrock-agentcore';
  return '';
}

/**
 * Extract the AWS region from a known AWS endpoint host, or `''` if the host
 * doesn't encode one. Mirrors the backend `extract_region_from_url`.
 */
export function extractAwsRegionFromUrl(url: string): string {
  const match =
    url.match(/\.lambda-url\.([a-z0-9-]+)\.on\.aws/) ??
    url.match(/\.execute-api\.([a-z0-9-]+)\.amazonaws\.com/) ??
    url.match(/\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com/);
  return match ? match[1] : '';
}
