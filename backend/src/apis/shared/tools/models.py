"""
Tool RBAC Models

Pydantic models for tool catalog, user tool access, and preferences.
Integrates with the existing AppRole RBAC system.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, model_validator


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


class GatewayListingMode(str, Enum):
    """How an AgentCore Gateway target lists its tools.

    Maps to the `bedrock-agentcore-control` `listingMode` enum (uppercased at
    the AWS boundary by GatewayTargetService). DYNAMIC resolves tools at call
    time but disables 3LO/OAuth and Gateway semantic search; DEFAULT lists
    tools statically and is required for both.
    """

    DEFAULT = "default"
    DYNAMIC = "dynamic"


class GatewayCredentialType(str, Enum):
    """How the Gateway authenticates outbound to a target's MCP endpoint.

    Maps to the `bedrock-agentcore-control` `credentialProviderType` enum.
    NONE registers a public endpoint with no outbound credentials (the API's
    `credentialProviderConfigurations` is omitted). GATEWAY_IAM_ROLE signs with
    the gateway's own execution role (SigV4) — for an mcpServer target this
    requires an explicit `iamCredentialProvider` naming the AWS service to sign
    for (see `aws_service`). OAUTH and API_KEY reference an existing AgentCore
    credential provider by ARN (provider provisioning is out of scope in v1).
    """

    NONE = "none"
    GATEWAY_IAM_ROLE = "gateway_iam_role"
    OAUTH = "oauth"
    API_KEY = "api_key"


class GatewayOAuthGrantType(str, Enum):
    """OAuth grant the Gateway uses when calling an OAUTH-credentialed target.

    Maps to the `bedrock-agentcore-control` `oauthCredentialProvider.grantType`
    enum. AUTHORIZATION_CODE is the on-behalf-of-user (3LO) flow that reuses our
    existing AgentCore Identity consent path (USER_FEDERATION); CLIENT_CREDENTIALS
    is machine-to-machine (2LO, no user consent). Only meaningful when
    credential_type is OAUTH.
    """

    AUTHORIZATION_CODE = "authorization_code"
    CLIENT_CREDENTIALS = "client_credentials"
    TOKEN_EXCHANGE = "token_exchange"


class ToolStatus(str, Enum):
    """Availability status of the tool."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    COMING_SOON = "coming_soon"


# =============================================================================
# External Tool Configuration Models
# =============================================================================


class MCPToolEntry(BaseModel):
    """A single tool exposed by an MCP server, with per-tool flags."""

    name: str = Field(..., description="Tool name as exposed by the MCP server")
    needs_approval: bool = Field(
        default=False,
        description="If true, the agent must request user confirmation before invoking this tool.",
    )
    description: Optional[str] = Field(
        None, description="Optional admin-supplied description for this tool"
    )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "needsApproval": self.needs_approval,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPToolEntry":
        return cls(
            name=data.get("name", ""),
            needs_approval=bool(data.get("needsApproval", False)),
            description=data.get("description"),
        )


def _parse_mcp_tools(raw: object) -> List[MCPToolEntry]:
    """Parse the mcp_config.tools field, accepting either the new entry-dict
    format or the legacy `List[str]` format written by older catalog rows.
    """
    if not isinstance(raw, list):
        return []
    entries: List[MCPToolEntry] = []
    for item in raw:
        if isinstance(item, MCPToolEntry):
            entries.append(item)
        elif isinstance(item, dict):
            entries.append(MCPToolEntry.from_dict(item))
        elif isinstance(item, str):
            entries.append(MCPToolEntry(name=item))
    return entries


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
    tools: List[MCPToolEntry] = Field(
        default_factory=list,
        description="Tools available on this MCP server, with per-tool flags. "
        "Empty means discover at runtime (no per-tool flags applied).",
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
            "tools": [entry.to_dict() for entry in self.tools],
            "healthCheckEnabled": self.health_check_enabled,
            "healthCheckIntervalSeconds": self.health_check_interval_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPServerConfig":
        """Create from dictionary. Accepts the legacy `List[str]` tools format
        for rows written before per-tool flags shipped — same precedent as the
        legacy-protocol mapping in ToolDefinition.from_dynamo_item."""
        return cls(
            server_url=data.get("serverUrl", ""),
            transport=data.get("transport", MCPTransport.STREAMABLE_HTTP),
            auth_type=data.get("authType", MCPAuthType.AWS_IAM),
            aws_region=data.get("awsRegion"),
            api_key_header=data.get("apiKeyHeader"),
            secret_arn=data.get("secretArn"),
            tools=_parse_mcp_tools(data.get("tools", [])),
            health_check_enabled=data.get("healthCheckEnabled", False),
            health_check_interval_seconds=data.get("healthCheckIntervalSeconds", 300),
        )

    def approval_required_names(self) -> set[str]:
        """Return the names of tools flagged as requiring user approval."""
        return {entry.name for entry in self.tools if entry.needs_approval}


class MCPGatewayConfig(BaseModel):
    """
    Configuration for an externally deployed MCP server registered as a
    target on the centralized AgentCore Gateway.

    Used when protocol is 'mcp' (ToolProtocol.MCP_GATEWAY). Unlike
    `MCPServerConfig` (protocol 'mcp_external', which the agent connects to
    directly), the agent never talks to this endpoint — the Gateway fronts it,
    and the tool reaches agents through the existing Gateway discovery path.

    `target_id` and `gateway_arn` are AWS-assigned identifiers stamped onto the
    config by the admin route after the Gateway target is created; they are the
    catalog↔AWS link that lets update/delete reconcile the live target.
    """

    # Gateway target identity
    target_name: str = Field(
        ..., description="Gateway target name (unique within the gateway)"
    )
    endpoint_url: str = Field(
        ..., description="External MCP server endpoint the Gateway calls"
    )
    listing_mode: GatewayListingMode = Field(
        default=GatewayListingMode.DEFAULT,
        description="How the Gateway lists this target's tools",
    )

    # Outbound auth from the Gateway to the target
    credential_type: GatewayCredentialType = Field(
        default=GatewayCredentialType.NONE,
        description="How the Gateway authenticates to the target endpoint",
    )
    credential_provider_arn: Optional[str] = Field(
        None,
        description="ARN of an existing AgentCore credential provider "
        "(required for OAUTH and API_KEY; unused for the others)",
    )
    aws_service: Optional[str] = Field(
        None,
        description="AWS service name for SigV4 signing (required for "
        "GATEWAY_IAM_ROLE on an mcpServer target, e.g. 'lambda', 'execute-api', "
        "'bedrock-agentcore'); unused for other credential types",
    )
    aws_region: Optional[str] = Field(
        None,
        description="AWS region for SigV4 signing (GATEWAY_IAM_ROLE only); "
        "defaults to the gateway's region when omitted",
    )
    lambda_function_name: Optional[str] = Field(
        None,
        description="Name (or ARN) of the Lambda backing the endpoint, for a "
        "GATEWAY_IAM_ROLE target on a Lambda Function URL. Lets the platform "
        "grant the gateway role InvokeFunctionUrl on exactly this function at "
        "registration (lambda:AddPermission) instead of a standing wildcard. "
        "Same-account only; cross-account targets must be public or use a "
        "credential provider.",
    )
    oauth_scopes: List[str] = Field(
        default_factory=list,
        description="OAuth scopes requested for OAUTH credential type",
    )
    grant_type: GatewayOAuthGrantType = Field(
        default=GatewayOAuthGrantType.AUTHORIZATION_CODE,
        description="OAuth grant for OAUTH credential type (3LO vs 2LO); "
        "ignored for other credential types",
    )
    custom_parameters: Optional[Dict[str, str]] = Field(
        None,
        description="Extra parameters forwarded to the OAuth provider. These are "
        "part of the AgentCore token-vault key, so they must match between "
        "target registration and token retrieval.",
    )

    # Per-tool flags (only applied when listing_mode is DEFAULT — DYNAMIC
    # listing resolves tool names at call time, so flags can't be matched)
    tools: List[MCPToolEntry] = Field(
        default_factory=list,
        description="Tools exposed by this target, with per-tool flags. "
        "Empty means rely on Gateway listing with no per-tool flags applied.",
    )

    # AWS-assigned identifiers (stamped on after target creation)
    target_id: Optional[str] = Field(
        None, description="Gateway target ID assigned by AgentCore on create"
    )
    gateway_arn: Optional[str] = Field(
        None, description="ARN of the gateway the target lives on"
    )

    model_config = {"use_enum_values": True}

    @model_validator(mode="after")
    def _validate_credentials(self) -> "MCPGatewayConfig":
        """Enforce the credential/listing-mode co-gating rules.

        OAuth (3LO) and Gateway semantic search require DEFAULT listing — see
        the listing-mode co-gating gotcha in issue #419. GATEWAY_IAM_ROLE uses
        the gateway's execution role and takes no provider ARN.
        """
        if self.credential_type == GatewayCredentialType.OAUTH:
            if not self.credential_provider_arn:
                raise ValueError(
                    "credential_type 'oauth' requires credential_provider_arn"
                )
            if self.listing_mode != GatewayListingMode.DEFAULT:
                raise ValueError(
                    "OAuth (3LO) targets require listing_mode 'default'; "
                    "'dynamic' disables 3LO and Gateway semantic search"
                )
        elif self.credential_type == GatewayCredentialType.API_KEY:
            if not self.credential_provider_arn:
                raise ValueError(
                    "credential_type 'api_key' requires credential_provider_arn"
                )
        elif self.credential_type == GatewayCredentialType.GATEWAY_IAM_ROLE:
            if self.credential_provider_arn:
                raise ValueError(
                    "credential_type 'gateway_iam_role' signs with the gateway "
                    "execution role and must not set credential_provider_arn"
                )
            if not self.aws_service:
                raise ValueError(
                    "credential_type 'gateway_iam_role' requires aws_service "
                    "(the AWS service name for SigV4 signing, e.g. 'lambda', "
                    "'execute-api', 'bedrock-agentcore') — an mcpServer target's "
                    "IAM credential provider must name the service to sign for"
                )
        return self

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "targetName": self.target_name,
            "endpointUrl": self.endpoint_url,
            "listingMode": self.listing_mode
            if isinstance(self.listing_mode, str)
            else self.listing_mode.value,
            "credentialType": self.credential_type
            if isinstance(self.credential_type, str)
            else self.credential_type.value,
            "credentialProviderArn": self.credential_provider_arn,
            "awsService": self.aws_service,
            "awsRegion": self.aws_region,
            "lambdaFunctionName": self.lambda_function_name,
            "oauthScopes": list(self.oauth_scopes),
            "grantType": self.grant_type
            if isinstance(self.grant_type, str)
            else self.grant_type.value,
            "customParameters": self.custom_parameters,
            "tools": [entry.to_dict() for entry in self.tools],
            "targetId": self.target_id,
            "gatewayArn": self.gateway_arn,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPGatewayConfig":
        """Create from dictionary."""
        return cls(
            target_name=data.get("targetName", ""),
            endpoint_url=data.get("endpointUrl", ""),
            listing_mode=data.get("listingMode", GatewayListingMode.DEFAULT),
            credential_type=data.get(
                "credentialType", GatewayCredentialType.NONE
            ),
            credential_provider_arn=data.get("credentialProviderArn"),
            aws_service=data.get("awsService"),
            aws_region=data.get("awsRegion"),
            lambda_function_name=data.get("lambdaFunctionName"),
            oauth_scopes=data.get("oauthScopes") or [],
            grant_type=data.get(
                "grantType", GatewayOAuthGrantType.AUTHORIZATION_CODE
            ),
            custom_parameters=data.get("customParameters"),
            tools=_parse_mcp_tools(data.get("tools", [])),
            target_id=data.get("targetId"),
            gateway_arn=data.get("gatewayArn"),
        )

    def approval_required_names(self) -> set[str]:
        """Return the names of tools flagged as requiring user approval."""
        return {entry.name for entry in self.tools if entry.needs_approval}


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
# MCP Apps — tool UI metadata (SEP-1865)
# =============================================================================

# Spec values for `_meta.ui.visibility`. "model" = the model may see/call the
# tool; "app" = an embedded MCP App may call it. Default per spec is both.
ToolVisibility = Literal["model", "app"]
DEFAULT_TOOL_VISIBILITY: List[ToolVisibility] = ["model", "app"]


class ToolUIMetadata(BaseModel):
    """Parsed `_meta.ui` from an MCP `tools/list` entry (MCP Apps / SEP-1865).

    PR #2 of the MCP Apps host-renderer initiative only consumes
    `resource_uri` and `visibility`. The full `_meta.ui` payload is retained
    verbatim in `raw` so later PRs (the `resources/read` fetch path, CSP /
    permissions handling, the postMessage bridge) can read it without another
    server round-trip. `_meta` is discovered live from the server, not
    admin-configured, so this never round-trips through DynamoDB.
    """

    resource_uri: Optional[str] = Field(
        None,
        description="The `ui://` URI from `_meta.ui.resourceUri` (fetched via "
        "`resources/read` in a later PR; never inlined).",
    )
    visibility: List[ToolVisibility] = Field(
        default_factory=lambda: list(DEFAULT_TOOL_VISIBILITY),
        description="Surfaces allowed to see/invoke the tool. Defaults to "
        "['model', 'app'] when the server omits `visibility`.",
    )
    raw: Dict[str, Any] = Field(
        default_factory=dict,
        description="Verbatim `_meta.ui` payload as returned by the server.",
    )

    model_config = {"use_enum_values": True}

    @classmethod
    def from_meta(cls, meta: Optional[Dict[str, Any]]) -> Optional["ToolUIMetadata"]:
        """Parse a tool's `_meta` dict into `ToolUIMetadata`.

        Returns None when the tool carries no `_meta.ui` block (an ordinary,
        non-UI tool). An absent `visibility` defaults to the spec default
        (`['model', 'app']`); an explicitly present `visibility` is honored
        as-is (so `[]` or `['app']` correctly hides the tool from the model).
        """
        if not isinstance(meta, dict):
            return None
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return None

        raw_visibility = ui.get("visibility")
        if isinstance(raw_visibility, list):
            visibility: List[ToolVisibility] = [
                v for v in raw_visibility if v in ("model", "app")
            ]
        else:
            visibility = list(DEFAULT_TOOL_VISIBILITY)

        resource_uri = ui.get("resourceUri")
        return cls(
            resource_uri=resource_uri if isinstance(resource_uri, str) else None,
            visibility=visibility,
            raw=dict(ui),
        )

    def visible_to_model(self) -> bool:
        """True if the model is allowed to see/call this tool."""
        return "model" in self.visibility

    def visible_to_app(self) -> bool:
        """True if an embedded MCP App may call this tool (SEP-1865).

        PR #5 gates the app-initiated `tools/call` proxy on this at both
        the app-api boundary and the inference-api dispatch (spec MUST:
        reject `tools/call` from apps for tools whose visibility excludes
        `"app"`).
        """
        return "app" in self.visibility


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
        ..., description="Unique identifier (e.g., 'fetch_url_content')"
    )

    # Display metadata
    display_name: str = Field(
        ..., description="Human-readable name (e.g., 'URL Fetcher')"
    )
    description: str = Field(..., description="Description of what the tool does")
    category: ToolCategory = Field(default=ToolCategory.UTILITY)

    # Technical metadata
    protocol: ToolProtocol = Field(..., description="How the tool is invoked")
    status: ToolStatus = Field(default=ToolStatus.ACTIVE)
    requires_oauth_provider: Optional[str] = Field(
        None,
        description="OAuth provider ID if tool requires user OAuth connection (e.g., 'google_workspace')",
    )
    forward_auth_token: bool = Field(
        default=False,
        description="If true, forward the user's OIDC authentication token to the MCP server. "
        "Only use for same-team controlled servers. Mutually exclusive with requires_oauth_provider.",
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
    mcp_gateway_config: Optional[MCPGatewayConfig] = Field(
        None,
        description="Gateway target configuration (required when protocol is 'mcp')",
    )

    # MCP Apps (SEP-1865) — derived live from the MCP server's `tools/list`
    # `_meta.ui`, not admin-configured, so these are intentionally NOT
    # round-tripped through DynamoDB. Defaults make the field inert for every
    # existing tool (full visibility, no UI resource).
    visibility: List[ToolVisibility] = Field(
        default_factory=lambda: list(DEFAULT_TOOL_VISIBILITY),
        description="Surfaces allowed to see/invoke this tool, from "
        "`_meta.ui.visibility`. Defaults to ['model', 'app'].",
    )
    ui_metadata: Optional[ToolUIMetadata] = Field(
        None,
        description="Parsed `_meta.ui` block when the tool ships an MCP App "
        "UI resource; None for ordinary tools.",
    )

    # Audit
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = Field(
        None, description="User ID of admin who created this entry"
    )
    updated_by: Optional[str] = Field(
        None, description="User ID of admin who last updated this"
    )

    model_config = {"use_enum_values": True}

    def curated_tool_names(self) -> Optional[Set[str]]:
        """The MCP tool names this catalog tool exposes, when known.

        For an external-MCP (``mcp_external``) or Gateway (``mcp``) tool the
        admin may curate the server's tool list; return those names so callers
        can validate a per-tool selection (``tool_id::name``) against them.
        Returns ``None`` when the tool is not an MCP server or has no curated
        list (e.g. a DYNAMIC gateway target, or a server whose tools are
        discovered live) — in which case per-tool names can't be validated
        statically.
        """
        cfg = self.mcp_config or self.mcp_gateway_config
        entries = getattr(cfg, "tools", None) if cfg else None
        if not entries:
            return None
        return {entry.name for entry in entries}

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
            "protocol": self.protocol if isinstance(self.protocol, str) else self.protocol.value,
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "requiresOauthProvider": self.requires_oauth_provider,
            "forwardAuthToken": self.forward_auth_token,
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
        if self.mcp_gateway_config:
            item["mcpGatewayConfig"] = self.mcp_gateway_config.to_dict()

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

        mcp_gateway_config = None
        if item.get("mcpGatewayConfig"):
            mcp_gateway_config = MCPGatewayConfig.from_dict(item["mcpGatewayConfig"])

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
            protocol=protocol,
            status=item.get("status", ToolStatus.ACTIVE),
            requires_oauth_provider=item.get("requiresOauthProvider"),
            forward_auth_token=item.get("forwardAuthToken", False),
            is_public=item.get("isPublic", False),
            enabled_by_default=item.get("enabledByDefault", False),
            mcp_config=mcp_config,
            a2a_config=a2a_config,
            mcp_gateway_config=mcp_gateway_config,
            created_at=datetime.fromisoformat(created_at.rstrip("Z")) if created_at else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.now(timezone.utc),
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
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
            updated_at=datetime.fromisoformat(updated_at.rstrip("Z")) if updated_at else datetime.now(timezone.utc),
        )


# =============================================================================
# API Response Models
# =============================================================================


class UserToolServerTool(BaseModel):
    """One tool exposed by an MCP-server catalog tool.

    Surfaced on ``UserToolAccess`` so the user-facing tools UI can enable a
    subset of a server's tools (per-tool enablement). ``name`` is the raw MCP
    tool name; a preference for it is keyed ``<tool_id>::<name>``. ``enabled``
    is the user's effective state for this individual tool (scoped preference,
    falling back to the server-level preference, then the catalog default).
    """

    name: str
    description: Optional[str] = None
    needs_approval: bool = Field(default=False, alias="needsApproval")
    enabled: bool = True

    model_config = {"populate_by_name": True}


class UserToolAccess(BaseModel):
    """
    Computed tool access for a specific user.
    Returned by the GET /tools endpoint.
    """

    tool_id: str = Field(..., alias="toolId")
    display_name: str = Field(..., alias="displayName")
    description: str
    category: ToolCategory
    protocol: ToolProtocol
    status: ToolStatus
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")

    # For MCP-server tools (protocol 'mcp'/'mcp_external'): the individual tools
    # the server exposes, so the UI can offer per-tool enablement. Empty for
    # non-MCP tools or servers whose tools are discovered live.
    server_tools: List[UserToolServerTool] = Field(
        default_factory=list, alias="serverTools"
    )

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


class MCPToolEntryPayload(BaseModel):
    """Wire-format for an MCPToolEntry on the admin API."""

    name: str
    needs_approval: bool = Field(default=False, alias="needsApproval")
    description: Optional[str] = None

    model_config = {"populate_by_name": True}

    def to_model(self) -> MCPToolEntry:
        return MCPToolEntry(
            name=self.name,
            needs_approval=self.needs_approval,
            description=self.description,
        )

    @classmethod
    def from_model(cls, entry: MCPToolEntry) -> "MCPToolEntryPayload":
        return cls(
            name=entry.name,
            needs_approval=entry.needs_approval,
            description=entry.description,
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
    tools: List[MCPToolEntryPayload] = Field(default_factory=list)
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
            tools=[entry.to_model() for entry in self.tools],
            health_check_enabled=self.health_check_enabled,
            health_check_interval_seconds=self.health_check_interval_seconds,
        )


class MCPGatewayConfigRequest(BaseModel):
    """Request body for Gateway target configuration (protocol 'mcp').

    Does not accept `targetId`/`gatewayArn` — those are AWS-assigned and
    stamped onto the stored config by the admin route after the Gateway
    target is created.
    """

    target_name: str = Field(..., alias="targetName")
    endpoint_url: str = Field(..., alias="endpointUrl")
    listing_mode: GatewayListingMode = Field(
        default=GatewayListingMode.DEFAULT, alias="listingMode"
    )
    credential_type: GatewayCredentialType = Field(
        default=GatewayCredentialType.NONE, alias="credentialType"
    )
    credential_provider_arn: Optional[str] = Field(
        None, alias="credentialProviderArn"
    )
    aws_service: Optional[str] = Field(None, alias="awsService")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    lambda_function_name: Optional[str] = Field(None, alias="lambdaFunctionName")
    oauth_scopes: List[str] = Field(default_factory=list, alias="oauthScopes")
    grant_type: GatewayOAuthGrantType = Field(
        default=GatewayOAuthGrantType.AUTHORIZATION_CODE, alias="grantType"
    )
    custom_parameters: Optional[Dict[str, str]] = Field(
        None, alias="customParameters"
    )
    tools: List[MCPToolEntryPayload] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "use_enum_values": True}

    def to_model(self) -> MCPGatewayConfig:
        """Convert to MCPGatewayConfig model (runs the co-gating validator)."""
        return MCPGatewayConfig(
            target_name=self.target_name,
            endpoint_url=self.endpoint_url,
            listing_mode=self.listing_mode,
            credential_type=self.credential_type,
            credential_provider_arn=self.credential_provider_arn,
            aws_service=self.aws_service,
            aws_region=self.aws_region,
            lambda_function_name=self.lambda_function_name,
            oauth_scopes=self.oauth_scopes,
            grant_type=self.grant_type,
            custom_parameters=self.custom_parameters,
            tools=[entry.to_model() for entry in self.tools],
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
    protocol: ToolProtocol = Field(default=ToolProtocol.LOCAL)
    status: ToolStatus = Field(default=ToolStatus.ACTIVE)
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: bool = Field(default=False, alias="forwardAuthToken")
    is_public: bool = Field(default=False, alias="isPublic")
    enabled_by_default: bool = Field(default=False, alias="enabledByDefault")

    # External tool configurations (optional based on protocol)
    mcp_config: Optional[MCPServerConfigRequest] = Field(None, alias="mcpConfig")
    a2a_config: Optional[A2AAgentConfigRequest] = Field(None, alias="a2aConfig")
    mcp_gateway_config: Optional[MCPGatewayConfigRequest] = Field(
        None, alias="mcpGatewayConfig"
    )

    model_config = {"populate_by_name": True}


class ToolUpdateRequest(BaseModel):
    """Request body for PUT /api/admin/tools/{tool_id}."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=100, alias="displayName"
    )
    description: Optional[str] = Field(None, max_length=500)
    category: Optional[ToolCategory] = None
    protocol: Optional[ToolProtocol] = None
    status: Optional[ToolStatus] = None
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: Optional[bool] = Field(None, alias="forwardAuthToken")
    is_public: Optional[bool] = Field(None, alias="isPublic")
    enabled_by_default: Optional[bool] = Field(None, alias="enabledByDefault")

    # External tool configurations (optional based on protocol)
    mcp_config: Optional[MCPServerConfigRequest] = Field(None, alias="mcpConfig")
    a2a_config: Optional[A2AAgentConfigRequest] = Field(None, alias="a2aConfig")
    mcp_gateway_config: Optional[MCPGatewayConfigRequest] = Field(
        None, alias="mcpGatewayConfig"
    )

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
    tools: List[MCPToolEntryPayload] = Field(default_factory=list)
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
            tools=[MCPToolEntryPayload.from_model(entry) for entry in config.tools],
            health_check_enabled=config.health_check_enabled,
            health_check_interval_seconds=config.health_check_interval_seconds,
        )


class MCPGatewayConfigResponse(BaseModel):
    """Response model for Gateway target configuration (protocol 'mcp').

    Includes the AWS-assigned `targetId`/`gatewayArn` so the admin UI can show
    the catalog↔Gateway linkage.
    """

    target_name: str = Field(..., alias="targetName")
    endpoint_url: str = Field(..., alias="endpointUrl")
    listing_mode: str = Field(..., alias="listingMode")
    credential_type: str = Field(..., alias="credentialType")
    credential_provider_arn: Optional[str] = Field(
        None, alias="credentialProviderArn"
    )
    aws_service: Optional[str] = Field(None, alias="awsService")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    lambda_function_name: Optional[str] = Field(None, alias="lambdaFunctionName")
    oauth_scopes: List[str] = Field(default_factory=list, alias="oauthScopes")
    grant_type: str = Field(..., alias="grantType")
    custom_parameters: Optional[Dict[str, str]] = Field(
        None, alias="customParameters"
    )
    tools: List[MCPToolEntryPayload] = Field(default_factory=list)
    target_id: Optional[str] = Field(None, alias="targetId")
    gateway_arn: Optional[str] = Field(None, alias="gatewayArn")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_model(cls, config: MCPGatewayConfig) -> "MCPGatewayConfigResponse":
        """Create response from MCPGatewayConfig model."""
        return cls(
            target_name=config.target_name,
            endpoint_url=config.endpoint_url,
            listing_mode=config.listing_mode
            if isinstance(config.listing_mode, str)
            else config.listing_mode.value,
            credential_type=config.credential_type
            if isinstance(config.credential_type, str)
            else config.credential_type.value,
            credential_provider_arn=config.credential_provider_arn,
            aws_service=config.aws_service,
            aws_region=config.aws_region,
            lambda_function_name=config.lambda_function_name,
            oauth_scopes=list(config.oauth_scopes),
            grant_type=config.grant_type
            if isinstance(config.grant_type, str)
            else config.grant_type.value,
            custom_parameters=config.custom_parameters,
            tools=[MCPToolEntryPayload.from_model(entry) for entry in config.tools],
            target_id=config.target_id,
            gateway_arn=config.gateway_arn,
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
    protocol: ToolProtocol
    status: ToolStatus
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: bool = Field(default=False, alias="forwardAuthToken")
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
    mcp_gateway_config: Optional[MCPGatewayConfigResponse] = Field(
        None, alias="mcpGatewayConfig"
    )

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

        mcp_gateway_config_response = None
        if tool.mcp_gateway_config:
            mcp_gateway_config_response = MCPGatewayConfigResponse.from_model(
                tool.mcp_gateway_config
            )

        return cls(
            tool_id=tool.tool_id,
            display_name=tool.display_name,
            description=tool.description,
            category=tool.category,
            protocol=tool.protocol,
            status=tool.status,
            requires_oauth_provider=tool.requires_oauth_provider,
            forward_auth_token=tool.forward_auth_token,
            is_public=tool.is_public,
            allowed_app_roles=allowed_roles or tool.allowed_app_roles,
            enabled_by_default=tool.enabled_by_default,
            created_at=tool.created_at.isoformat() + "Z" if tool.created_at else "",
            updated_at=tool.updated_at.isoformat() + "Z" if tool.updated_at else "",
            created_by=tool.created_by,
            updated_by=tool.updated_by,
            mcp_config=mcp_config_response,
            a2a_config=a2a_config_response,
            mcp_gateway_config=mcp_gateway_config_response,
        )


class AdminToolListResponse(BaseModel):
    """Response for GET /api/admin/tools."""

    tools: List[AdminToolResponse]
    total: int


class MCPDiscoverRequest(BaseModel):
    """Request body for POST /api/admin/tools/discover.

    Same fields as MCPServerConfigRequest minus the `tools` list — the
    point of discovery is to populate that list. Provider-gated OAuth (3LO)
    servers can't be discovered admin-side (no end-user provider token
    available); the route returns a 400 in that case.

    `forward_auth_token` mirrors the catalog flag of the same name: when set,
    the route signs the discovery request with the *admin's own* OIDC token
    instead of SigV4, matching how the agent loop forwards the end-user's
    token at runtime. This lets a same-team MCP server that validates a
    forwarded JWT (Lambda Function URL AuthType=NONE) be discovered without
    any IAM invoke permission.
    """

    server_url: str = Field(..., alias="serverUrl")
    transport: MCPTransport = Field(default=MCPTransport.STREAMABLE_HTTP)
    auth_type: MCPAuthType = Field(default=MCPAuthType.AWS_IAM, alias="authType")
    aws_region: Optional[str] = Field(None, alias="awsRegion")
    api_key_header: Optional[str] = Field(None, alias="apiKeyHeader")
    secret_arn: Optional[str] = Field(None, alias="secretArn")
    forward_auth_token: bool = Field(default=False, alias="forwardAuthToken")

    model_config = {"populate_by_name": True, "use_enum_values": True}

    def to_config(self) -> MCPServerConfig:
        return MCPServerConfig(
            server_url=self.server_url,
            transport=self.transport,
            auth_type=self.auth_type,
            aws_region=self.aws_region,
            api_key_header=self.api_key_header,
            secret_arn=self.secret_arn,
            tools=[],
        )


class DiscoveredMCPTool(BaseModel):
    """A tool discovered from a live MCP server's list_tools call."""

    name: str
    description: Optional[str] = None


class MCPDiscoverResponse(BaseModel):
    """Response body for POST /api/admin/tools/discover."""

    tools: List[DiscoveredMCPTool]


class GatewayTargetStatusResponse(BaseModel):
    """Live health of the Gateway target backing a protocol='mcp' tool.

    Response body for GET /api/admin/tools/{tool_id}/gateway-status. The
    AgentCore Gateway connects to and lists tools from the target
    asynchronously after registration, so the catalog row alone can't tell an
    admin whether the target is usable. `status` is the gateway target status
    (CREATING / READY / FAILED / UPDATE_UNSUCCESSFUL / …); `status_reasons`
    carries the gateway's explanation when unhealthy. `MISSING` is a synthetic
    status used when the catalog references a target that no longer exists on
    the gateway. `healthy` is a convenience the badge can render directly."""

    target_id: str = Field(..., alias="targetId")
    status: str
    status_reasons: List[str] = Field(default_factory=list, alias="statusReasons")
    healthy: bool

    model_config = {"populate_by_name": True}


