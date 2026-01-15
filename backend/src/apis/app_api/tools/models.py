"""
Tool RBAC Models

Pydantic models for tool catalog, user tool access, and preferences.
Integrates with the existing AppRole RBAC system.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ToolCategory(str, Enum):
    """Categories for organizing tools in the UI."""

    SEARCH = "search"
    DATA = "data"
    VISUALIZATION = "visualization"
    DOCUMENT = "document"
    CODE = "code"
    BROWSER = "browser"
    UTILITY = "utility"
    RESEARCH = "research"
    FINANCE = "finance"
    GATEWAY = "gateway"
    CUSTOM = "custom"


class ToolProtocol(str, Enum):
    """Protocol used to invoke the tool."""

    LOCAL = "local"  # Direct function call
    AWS_SDK = "aws_sdk"  # AWS Bedrock services
    MCP_GATEWAY = "mcp"  # MCP via AgentCore Gateway
    MCP_EXTERNAL = "mcp_external"  # MCP via externally deployed server
    A2A = "a2a"  # Agent-to-Agent


class MCPTransport(str, Enum):
    """Transport type for MCP servers."""

    STREAMABLE_HTTP = "streamable-http"  # Streamable HTTP (default for Lambda)
    SSE = "sse"  # Server-Sent Events
    STDIO = "stdio"  # Standard I/O (local only)


class MCPAuthType(str, Enum):
    """Authentication type for MCP servers."""

    NONE = "none"  # No authentication
    AWS_IAM = "aws-iam"  # AWS IAM SigV4 signing
    API_KEY = "api-key"  # API key header
    BEARER_TOKEN = "bearer-token"  # Bearer token authentication
    OAUTH2 = "oauth2"  # OAuth 2.0 client credentials


class A2AAuthType(str, Enum):
    """Authentication type for Agent-to-Agent communication."""

    NONE = "none"
    AWS_IAM = "aws-iam"
    AGENTCORE = "agentcore"  # AgentCore Runtime auth
    API_KEY = "api-key"


class ToolStatus(str, Enum):
    """Availability status of the tool."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    COMING_SOON = "coming_soon"


# =============================================================================
# External Tool Configuration Models
# =============================================================================


class MCPServerConfig(BaseModel):
    """
    Configuration for external MCP server connections.

    Used when protocol is 'mcp_external' to define how to connect
    to an externally deployed MCP server (Lambda, API Gateway, etc.)
    """

    # Server endpoint
    server_url: str = Field(
        ..., description="MCP server URL (Lambda Function URL or API Gateway)"
    )
    transport: MCPTransport = Field(
        default=MCPTransport.STREAMABLE_HTTP,
        description="Transport type for MCP communication",
    )

    # Authentication
    auth_type: MCPAuthType = Field(
        default=MCPAuthType.AWS_IAM, description="Authentication method"
    )
    aws_region: Optional[str] = Field(
        None, description="AWS region for SigV4 auth (extracted from URL if not set)"
    )
    api_key_header: Optional[str] = Field(
        None, description="Header name for API key auth (default: x-api-key)"
    )
    secret_arn: Optional[str] = Field(
        None,
        description="Secrets Manager ARN for credentials (API key, OAuth client secrets)",
    )

    # MCP tool discovery
    tools: List[str] = Field(
        default_factory=list,
        description="List of tool names available on this MCP server. Empty means discover at runtime.",
    )

    # Health check
    health_check_enabled: bool = Field(
        default=False, description="Enable health checks for this server"
    )
    health_check_interval_seconds: int = Field(
        default=300, description="Interval between health checks"
    )

    model_config = {"use_enum_values": True}

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "serverUrl": self.server_url,
            "transport": self.transport
            if isinstance(self.transport, str)
            else self.transport.value,
            "authType": self.auth_type
            if isinstance(self.auth_type, str)
            else self.auth_type.value,
            "awsRegion": self.aws_region,
            "apiKeyHeader": self.api_key_header,
            "secretArn": self.secret_arn,
            "tools": self.tools,
            "healthCheckEnabled": self.health_check_enabled,
            "healthCheckIntervalSeconds": self.health_check_interval_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPServerConfig":
        """Create from dictionary."""
        return cls(
            server_url=data.get("serverUrl", ""),
            transport=data.get("transport", MCPTransport.STREAMABLE_HTTP),
            auth_type=data.get("authType", MCPAuthType.AWS_IAM),
            aws_region=data.get("awsRegion"),
            api_key_header=data.get("apiKeyHeader"),
            secret_arn=data.get("secretArn"),
            tools=data.get("tools", []),
            health_check_enabled=data.get("healthCheckEnabled", False),
            health_check_interval_seconds=data.get("healthCheckIntervalSeconds", 300),
        )


class A2AAgentConfig(BaseModel):
    """
    Configuration for Agent-to-Agent communication.

    Used when protocol is 'a2a' to define how to communicate
    with a remote agent via AgentCore Runtime or direct HTTP.
    """

    # Agent endpoint
    agent_url: str = Field(..., description="Remote agent endpoint URL")
    agent_id: Optional[str] = Field(
        None, description="AgentCore Runtime agent ID (if using AgentCore)"
    )

    # Authentication
    auth_type: A2AAuthType = Field(
        default=A2AAuthType.AGENTCORE, description="Authentication method"
    )
    aws_region: Optional[str] = Field(None, description="AWS region for auth")
    secret_arn: Optional[str] = Field(
        None, description="Secrets Manager ARN for credentials"
    )

    # Agent capabilities
    capabilities: List[str] = Field(
        default_factory=list,
        description="List of capabilities/skills this agent provides",
    )

    # Communication settings
    timeout_seconds: int = Field(
        default=120, description="Request timeout in seconds"
    )
    max_retries: int = Field(default=3, description="Maximum retry attempts")

    model_config = {"use_enum_values": True}

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "agentUrl": self.agent_url,
            "agentId": self.agent_id,
            "authType": self.auth_type
            if isinstance(self.auth_type, str)
            else self.auth_type.value,
            "awsRegion": self.aws_region,
            "secretArn": self.secret_arn,
            "capabilities": self.capabilities,
            "timeoutSeconds": self.timeout_seconds,
            "maxRetries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "A2AAgentConfig":
        """Create from dictionary."""
        return cls(
            agent_url=data.get("agentUrl", ""),
            agent_id=data.get("agentId"),
            auth_type=data.get("authType", A2AAuthType.AGENTCORE),
            aws_region=data.get("awsRegion"),
            secret_arn=data.get("secretArn"),
            capabilities=data.get("capabilities", []),
            timeout_seconds=data.get("timeoutSeconds", 120),
            max_retries=data.get("maxRetries", 3),
        )


# =============================================================================
# Database Models (stored in DynamoDB)
# =============================================================================


class ToolDefinition(BaseModel):
    """
    Catalog entry for a tool stored in DynamoDB.

    NOTE: Access control is managed via AppRoles, not stored directly on tools.
    The `allowed_app_roles` field is computed for display purposes only.
    """

    # Identity
    tool_id: str = Field(
        ..., description="Unique identifier (e.g., 'get_current_weather')"
    )

    # Display metadata
    display_name: str = Field(
        ..., description="Human-readable name (e.g., 'Weather Lookup')"
    )
    description: str = Field(..., description="Description of what the tool does")
    category: ToolCategory = Field(default=ToolCategory.UTILITY)
    icon: Optional[str] = Field(
        None, description="Icon identifier for UI (e.g., 'heroCloud')"
    )

    # Technical metadata
    protocol: ToolProtocol = Field(..., description="How the tool is invoked")
    status: ToolStatus = Field(default=ToolStatus.ACTIVE)
    requires_api_key: bool = Field(
        default=False, description="Whether tool requires external API key"
    )

    # Access control
    is_public: bool = Field(
        default=False,
        description="If true, tool is available to all authenticated users regardless of role",
    )

    # Computed field - which AppRoles grant this tool (for admin UI display)
    allowed_app_roles: List[str] = Field(
        default_factory=list,
        description="AppRole IDs that grant access to this tool (computed from AppRoles)",
    )

    # Default behavior
    enabled_by_default: bool = Field(
        default=False,
        description="If true, tool is enabled when user first accesses it",
    )

    # External tool configuration (protocol-specific)
    mcp_config: Optional[MCPServerConfig] = Field(
        None,
        description="MCP server configuration (required when protocol is 'mcp_external')",
    )
    a2a_config: Optional[A2AAgentConfig] = Field(
        None,
        description="A2A agent configuration (required when protocol is 'a2a')",
    )

    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = Field(
        None, description="User ID of admin who created this entry"
    )
    updated_by: Optional[str] = Field(
        None, description="User ID of admin who last updated this"
    )

    model_config = {"use_enum_values": True}

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        item = {
            "PK": f"TOOL#{self.tool_id}",
            "SK": "METADATA",
            "GSI1PK": f"CATEGORY#{self.category}",
            "GSI1SK": f"TOOL#{self.tool_id}",
            "toolId": self.tool_id,
            "displayName": self.display_name,
            "description": self.description,
            "category": self.category if isinstance(self.category, str) else self.category.value,
            "icon": self.icon,
            "protocol": self.protocol if isinstance(self.protocol, str) else self.protocol.value,
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "requiresApiKey": self.requires_api_key,
            "isPublic": self.is_public,
            "enabledByDefault": self.enabled_by_default,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "createdBy": self.created_by,
            "updatedBy": self.updated_by,
        }

        # Add external tool configurations if present
        if self.mcp_config:
            item["mcpConfig"] = self.mcp_config.to_dict()
        if self.a2a_config:
            item["a2aConfig"] = self.a2a_config.to_dict()

        return item

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "ToolDefinition":
        """Create from DynamoDB item."""
        created_at = item.get("createdAt")
        updated_at = item.get("updatedAt")

        # Parse external tool configurations if present
        mcp_config = None
        if item.get("mcpConfig"):
            mcp_config = MCPServerConfig.from_dict(item["mcpConfig"])

        a2a_config = None
        if item.get("a2aConfig"):
            a2a_config = A2AAgentConfig.from_dict(item["a2aConfig"])

        # Handle legacy protocol values gracefully
        protocol_value = item.get("protocol", ToolProtocol.LOCAL)
        try:
            if isinstance(protocol_value, str):
                # Map legacy protocol values to new enum
                protocol_mapping = {
                    "mcp_http": ToolProtocol.MCP_EXTERNAL,  # Legacy value
                    "http": ToolProtocol.MCP_EXTERNAL,  # Legacy value
                }
                protocol_value = protocol_mapping.get(protocol_value, protocol_value)
                protocol = ToolProtocol(protocol_value)
            else:
                protocol = protocol_value
        except ValueError:
            # Unknown protocol, default to LOCAL
            protocol = ToolProtocol.LOCAL

        return cls(
            tool_id=item.get("toolId", ""),
            display_name=item.get("displayName", ""),
            description=item.get("description", ""),
            category=item.get("category", ToolCategory.UTILITY),
            icon=item.get("icon"),
            protocol=protocol,
            status=item.get("status", ToolStatus.ACTIVE),
            requires_api_key=item.get("requiresApiKey", False),
            is_public=item.get("isPublic", False),
            enabled_by_default=item.get("enabledByDefault", False),
            mcp_config=mcp_config,
            a2a_config=a2a_config,
            created_at=datetime.fromisoformat(created_at.rstrip("Z")) if created_at else datetime.utcnow(),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.utcnow(),
            created_by=item.get("createdBy"),
            updated_by=item.get("updatedBy"),
        )


class UserToolPreference(BaseModel):
    """
    User's explicit tool preferences stored per-user in DynamoDB.

    Overrides default enabled state for tools the user has access to.
    """

    user_id: str
    tool_preferences: Dict[str, bool] = Field(
        default_factory=dict, description="Map of tool_id -> enabled state"
    )
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dynamo_item(self) -> dict:
        """Convert to DynamoDB item format."""
        return {
            "PK": f"USER#{self.user_id}",
            "SK": "TOOL_PREFERENCES",
            "userId": self.user_id,
            "toolPreferences": self.tool_preferences,
            "updatedAt": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "UserToolPreference":
        """Create from DynamoDB item."""
        updated_at = item.get("updatedAt")
        return cls(
            user_id=item.get("userId", ""),
            tool_preferences=item.get("toolPreferences", {}),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.utcnow(),
        )


# =============================================================================
# API Response Models
# =============================================================================


class UserToolAccess(BaseModel):
    """
    Computed tool access for a specific user.
    Returned by the GET /tools endpoint.
    """

    tool_id: str = Field(..., alias="toolId")
    display_name: str = Field(..., alias="displayName")
    description: str
    category: ToolCategory
    icon: Optional[str] = None
    protocol: ToolProtocol
    status: ToolStatus

    # Access info
    granted_by: List[str] = Field(
        ...,
        alias="grantedBy",
        description="List of sources that grant access (e.g., ['public', 'power_user', 'researcher'])",
    )
    enabled_by_default: bool = Field(..., alias="enabledByDefault")

    # Current user state
    user_enabled: Optional[bool] = Field(
        None,
        alias="userEnabled",
        description="User's explicit preference (None = use default)",
    )
    is_enabled: bool = Field(
        ...,
        alias="isEnabled",
        description="Computed: user_enabled if set, else enabled_by_default",
    )

    model_config = {"populate_by_name": True, "use_enum_values": True}


class UserToolsResponse(BaseModel):
    """Response model for GET /api/tools endpoint."""

    tools: List[UserToolAccess]
    categories: List[str]
    app_roles_applied: List[str] = Field(..., alias="appRolesApplied")

    model_config = {"populate_by_name": True}


# =============================================================================
# API Request Models
# =============================================================================


class ToolPreferencesRequest(BaseModel):
    """Request body for PUT /api/tools/preferences."""

    preferences: Dict[str, bool] = Field(
        ..., description="Map of tool_id -> enabled state"
    )


class MCPServerConfigRequest(BaseModel):
    """Request body for MCP server configuration."""

    server_url: str = Field(..., alias="serverUrl")
    transport: MCPTransport = Field(
        default=MCPTransport.STREAMABLE_HTTP, alias="transport"
    )
    auth_type: MCPAuthType = Field(default=MCPAuthType.AWS_IAM, alias="authType")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    api_key_header: Optional[str] = Field(None, alias="apiKeyHeader")
    secret_arn: Optional[str] = Field(None, alias="secretArn")
    tools: List[str] = Field(default_factory=list)
    health_check_enabled: bool = Field(default=False, alias="healthCheckEnabled")
    health_check_interval_seconds: int = Field(
        default=300, alias="healthCheckIntervalSeconds"
    )

    model_config = {"populate_by_name": True, "use_enum_values": True}

    def to_model(self) -> MCPServerConfig:
        """Convert to MCPServerConfig model."""
        return MCPServerConfig(
            server_url=self.server_url,
            transport=self.transport,
            auth_type=self.auth_type,
            aws_region=self.aws_region,
            api_key_header=self.api_key_header,
            secret_arn=self.secret_arn,
            tools=self.tools,
            health_check_enabled=self.health_check_enabled,
            health_check_interval_seconds=self.health_check_interval_seconds,
        )


class A2AAgentConfigRequest(BaseModel):
    """Request body for A2A agent configuration."""

    agent_url: str = Field(..., alias="agentUrl")
    agent_id: Optional[str] = Field(None, alias="agentId")
    auth_type: A2AAuthType = Field(default=A2AAuthType.AGENTCORE, alias="authType")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    secret_arn: Optional[str] = Field(None, alias="secretArn")
    capabilities: List[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=120, alias="timeoutSeconds")
    max_retries: int = Field(default=3, alias="maxRetries")

    model_config = {"populate_by_name": True, "use_enum_values": True}

    def to_model(self) -> A2AAgentConfig:
        """Convert to A2AAgentConfig model."""
        return A2AAgentConfig(
            agent_url=self.agent_url,
            agent_id=self.agent_id,
            auth_type=self.auth_type,
            aws_region=self.aws_region,
            secret_arn=self.secret_arn,
            capabilities=self.capabilities,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )


class ToolCreateRequest(BaseModel):
    """Request body for POST /api/admin/tools."""

    tool_id: str = Field(
        ..., pattern=r"^[a-z][a-z0-9_]{2,49}$", alias="toolId"
    )
    display_name: str = Field(
        ..., min_length=1, max_length=100, alias="displayName"
    )
    description: str = Field(..., max_length=500)
    category: ToolCategory = Field(default=ToolCategory.UTILITY)
    icon: Optional[str] = Field(None, max_length=50)
    protocol: ToolProtocol = Field(default=ToolProtocol.LOCAL)
    status: ToolStatus = Field(default=ToolStatus.ACTIVE)
    requires_api_key: bool = Field(default=False, alias="requiresApiKey")
    is_public: bool = Field(default=False, alias="isPublic")
    enabled_by_default: bool = Field(default=False, alias="enabledByDefault")

    # External tool configurations (optional based on protocol)
    mcp_config: Optional[MCPServerConfigRequest] = Field(None, alias="mcpConfig")
    a2a_config: Optional[A2AAgentConfigRequest] = Field(None, alias="a2aConfig")

    model_config = {"populate_by_name": True}


class ToolUpdateRequest(BaseModel):
    """Request body for PUT /api/admin/tools/{tool_id}."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=100, alias="displayName"
    )
    description: Optional[str] = Field(None, max_length=500)
    category: Optional[ToolCategory] = None
    icon: Optional[str] = Field(None, max_length=50)
    protocol: Optional[ToolProtocol] = None
    status: Optional[ToolStatus] = None
    requires_api_key: Optional[bool] = Field(None, alias="requiresApiKey")
    is_public: Optional[bool] = Field(None, alias="isPublic")
    enabled_by_default: Optional[bool] = Field(None, alias="enabledByDefault")

    # External tool configurations (optional based on protocol)
    mcp_config: Optional[MCPServerConfigRequest] = Field(None, alias="mcpConfig")
    a2a_config: Optional[A2AAgentConfigRequest] = Field(None, alias="a2aConfig")

    model_config = {"populate_by_name": True}


class ToolRoleAssignment(BaseModel):
    """Role assignment info for a tool."""

    role_id: str = Field(..., alias="roleId")
    display_name: str = Field(..., alias="displayName")
    grant_type: str = Field(
        ..., alias="grantType", description="'direct' or 'inherited'"
    )
    inherited_from: Optional[str] = Field(None, alias="inheritedFrom")
    enabled: bool

    model_config = {"populate_by_name": True}


class ToolRolesResponse(BaseModel):
    """Response for GET /api/admin/tools/{tool_id}/roles."""

    tool_id: str = Field(..., alias="toolId")
    roles: List[ToolRoleAssignment]

    model_config = {"populate_by_name": True}


class SetToolRolesRequest(BaseModel):
    """Request body for PUT /api/admin/tools/{tool_id}/roles."""

    app_role_ids: List[str] = Field(..., alias="appRoleIds")

    model_config = {"populate_by_name": True}


class AddRemoveRolesRequest(BaseModel):
    """Request body for POST /api/admin/tools/{tool_id}/roles/add or /remove."""

    app_role_ids: List[str] = Field(..., alias="appRoleIds")

    model_config = {"populate_by_name": True}


class MCPServerConfigResponse(BaseModel):
    """Response model for MCP server configuration."""

    server_url: str = Field(..., alias="serverUrl")
    transport: str
    auth_type: str = Field(..., alias="authType")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    api_key_header: Optional[str] = Field(None, alias="apiKeyHeader")
    secret_arn: Optional[str] = Field(None, alias="secretArn")
    tools: List[str] = Field(default_factory=list)
    health_check_enabled: bool = Field(default=False, alias="healthCheckEnabled")
    health_check_interval_seconds: int = Field(
        default=300, alias="healthCheckIntervalSeconds"
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_model(cls, config: MCPServerConfig) -> "MCPServerConfigResponse":
        """Create response from MCPServerConfig model."""
        return cls(
            server_url=config.server_url,
            transport=config.transport
            if isinstance(config.transport, str)
            else config.transport.value,
            auth_type=config.auth_type
            if isinstance(config.auth_type, str)
            else config.auth_type.value,
            aws_region=config.aws_region,
            api_key_header=config.api_key_header,
            secret_arn=config.secret_arn,
            tools=config.tools,
            health_check_enabled=config.health_check_enabled,
            health_check_interval_seconds=config.health_check_interval_seconds,
        )


class A2AAgentConfigResponse(BaseModel):
    """Response model for A2A agent configuration."""

    agent_url: str = Field(..., alias="agentUrl")
    agent_id: Optional[str] = Field(None, alias="agentId")
    auth_type: str = Field(..., alias="authType")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    secret_arn: Optional[str] = Field(None, alias="secretArn")
    capabilities: List[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=120, alias="timeoutSeconds")
    max_retries: int = Field(default=3, alias="maxRetries")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_model(cls, config: A2AAgentConfig) -> "A2AAgentConfigResponse":
        """Create response from A2AAgentConfig model."""
        return cls(
            agent_url=config.agent_url,
            agent_id=config.agent_id,
            auth_type=config.auth_type
            if isinstance(config.auth_type, str)
            else config.auth_type.value,
            aws_region=config.aws_region,
            secret_arn=config.secret_arn,
            capabilities=config.capabilities,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
        )


class AdminToolResponse(BaseModel):
    """Response model for admin tool listing."""

    tool_id: str = Field(..., alias="toolId")
    display_name: str = Field(..., alias="displayName")
    description: str
    category: ToolCategory
    icon: Optional[str] = None
    protocol: ToolProtocol
    status: ToolStatus
    requires_api_key: bool = Field(..., alias="requiresApiKey")
    is_public: bool = Field(..., alias="isPublic")
    allowed_app_roles: List[str] = Field(..., alias="allowedAppRoles")
    enabled_by_default: bool = Field(..., alias="enabledByDefault")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: Optional[str] = Field(None, alias="createdBy")
    updated_by: Optional[str] = Field(None, alias="updatedBy")

    # External tool configurations
    mcp_config: Optional[MCPServerConfigResponse] = Field(None, alias="mcpConfig")
    a2a_config: Optional[A2AAgentConfigResponse] = Field(None, alias="a2aConfig")

    model_config = {"populate_by_name": True, "use_enum_values": True}

    @classmethod
    def from_tool_definition(
        cls, tool: ToolDefinition, allowed_roles: Optional[List[str]] = None
    ) -> "AdminToolResponse":
        """Create response from ToolDefinition."""
        # Convert external configs if present
        mcp_config_response = None
        if tool.mcp_config:
            mcp_config_response = MCPServerConfigResponse.from_model(tool.mcp_config)

        a2a_config_response = None
        if tool.a2a_config:
            a2a_config_response = A2AAgentConfigResponse.from_model(tool.a2a_config)

        return cls(
            tool_id=tool.tool_id,
            display_name=tool.display_name,
            description=tool.description,
            category=tool.category,
            icon=tool.icon,
            protocol=tool.protocol,
            status=tool.status,
            requires_api_key=tool.requires_api_key,
            is_public=tool.is_public,
            allowed_app_roles=allowed_roles or tool.allowed_app_roles,
            enabled_by_default=tool.enabled_by_default,
            created_at=tool.created_at.isoformat() + "Z" if tool.created_at else "",
            updated_at=tool.updated_at.isoformat() + "Z" if tool.updated_at else "",
            created_by=tool.created_by,
            updated_by=tool.updated_by,
            mcp_config=mcp_config_response,
            a2a_config=a2a_config_response,
        )


class AdminToolListResponse(BaseModel):
    """Response for GET /api/admin/tools."""

    tools: List[AdminToolResponse]
    total: int


class SyncResult(BaseModel):
    """Result of syncing tool catalog from registry."""

    discovered: List[dict] = Field(
        default_factory=list, description="Tools found in registry but not in catalog"
    )
    orphaned: List[dict] = Field(
        default_factory=list, description="Tools in catalog but not in registry"
    )
    unchanged: List[str] = Field(
        default_factory=list, description="Tools that exist in both"
    )
    dry_run: bool = Field(..., alias="dryRun")

    model_config = {"populate_by_name": True}
