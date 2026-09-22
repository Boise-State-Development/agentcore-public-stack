"""
Tool RBAC Models

Pydantic models for tool catalog, user tool access, and preferences.
Integrates with the existing AppRole RBAC system.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set, Union

from pydantic import BaseModel, Field, field_validator, model_validator
from apis.shared.timestamps import from_iso, to_iso

# EntityTypeIndex (GSI5) partition value for tool-catalog rows.
#
# The index is generic — "list every item of type X" on a table that mixes
# tools, skills, roles, role grants and one preferences row per user. Only the
# tool partition is written and read today; a second entity type can be added
# without another GSI, which matters because DynamoDB allows only one GSI
# creation per UpdateTable.
ENTITY_TYPE_TOOL = "ENTITY#TOOL"


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
    always_on: bool = Field(
        default=False,
        description=(
            "If true, this one tool of the server is pinned into every turn's "
            "effective toolset for users whose roles grant the server, and the "
            "user cannot turn it off. Enables, never grants. Prefer this over "
            "flagging the whole server: an always-on server puts every one of "
            "its tool schemas in the cacheable toolConfig for the life of every "
            "session. See docs/specs/admin-always-on-tools.md."
        ),
    )
    description: Optional[str] = Field(
        None, description="Optional admin-supplied description for this tool"
    )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "needsApproval": self.needs_approval,
            "alwaysOn": self.always_on,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPToolEntry":
        return cls(
            name=data.get("name", ""),
            needs_approval=bool(data.get("needsApproval", False)),
            # Absent on every row written before always-on shipped, so it reads
            # back False and the entry behaves exactly as it did.
            always_on=bool(data.get("alwaysOn", False)),
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
    retirement_note: Optional[str] = Field(
        None,
        description=(
            "What a user should do instead, shown wherever a non-active tool is "
            "surfaced (e.g. 'Replaced by Canvas for Faculty', 'No replacement — "
            "contact OIT'). Free text rather than a replacedBy tool id on purpose: "
            "a retirement often has no drop-in successor, or splits across several, "
            "and an id cannot say so. Display only — it never reaches the model's "
            "toolConfig, so it costs nothing per turn. "
            "See docs/specs/mcp-server-retirement.md §7."
        ),
    )
    retires_on: Optional[str] = Field(
        None,
        description=(
            "ISO date (YYYY-MM-DD) the tool stops working — i.e. when Stage 3 "
            "revokes its grant and every Agent binding it starts failing. Display "
            "only, and advisory: nothing schedules off it, because a retirement is "
            "driven by the runbook, not by a timer."
        ),
    )

    @field_validator("retirement_note", "retires_on", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Treat an empty or whitespace-only value as unset.

        The admin form posts ``""`` for an untouched optional input, and a tool
        carrying ``retirementNote: ""`` would render an empty reason line rather
        than none at all.
        """
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("retires_on")
    @classmethod
    def _validate_retires_on(cls, v: Optional[str]) -> Optional[str]:
        """Reject anything that is not a plain ISO date.

        Stored as a string rather than a ``date`` because it is displayed, never
        computed with, and a string round-trips through DynamoDB unchanged. That
        is exactly why it needs a guard on the way in: without one, "soon" or
        "9/30/26" would persist happily and reach the SPA as-is.
        """
        if v is None:
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"retiresOn must be an ISO date (YYYY-MM-DD), got '{v}'")
        return v

    requires_oauth_provider: Optional[str] = Field(
        None,
        description="OAuth provider ID if tool requires user OAuth connection (e.g., 'google_workspace')",
    )
    forward_auth_token: bool = Field(
        default=False,
        description="If true, forward the user's OIDC authentication token to the MCP server. "
        "Only use for same-team controlled servers. Mutually exclusive with requires_oauth_provider.",
    )
    token_exchange_audience: Optional[str] = Field(
        None,
        description="Token-service applicationId to exchange the user's token for (RFC 8693). "
        "When set, the runtime trades the user's Cognito access token for a token-service JWT "
        "scoped to this application and forwards that instead of the raw token — so APIs "
        "that already trust token-service JWTs can serve agent requests as the signed-in user. "
        "Takes precedence over forward_auth_token, which would send a token the target API "
        "cannot validate. Mutually exclusive with requires_oauth_provider.",
    )

    @field_validator("token_exchange_audience", mode="before")
    @classmethod
    def _clean_token_exchange_audience(cls, v: object) -> object:
        """
        Strip surrounding whitespace and treat blank as unset.

        A pasted audience arrived with a leading space, which made the token
        service refuse every exchange: it compares the audience against its
        allowlist with an ordinal comparison, so " <guid>" != "<guid>". The tool
        still appeared to work because the endpoint it called happened to allow
        anonymous access — the request simply went unauthenticated. Silent
        downgrade from delegated identity to anonymous is the worst possible
        failure mode for this feature, so the value is normalised on the way in
        rather than trusted.
        """
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

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
    always_on: bool = Field(
        default=False,
        description=(
            "If true, the tool is pinned into every turn's effective toolset "
            "for users whose roles grant it, and the user cannot turn it off. "
            "Enables, never grants: a user whose roles do not carry the tool is "
            "unaffected. See docs/specs/admin-always-on-tools.md."
        ),
    )

    @model_validator(mode="after")
    def _normalize_always_on(self) -> "ToolDefinition":
        """An always-on tool is necessarily on by default.

        ``enabled_by_default=False`` + ``always_on=True`` is incoherent — the
        tool is pinned on for everyone, so "off until the user turns it on"
        describes nothing. The spec chose two booleans over a three-state enum
        to avoid migrating every catalog row, request model and SPA model
        (docs/specs/admin-always-on-tools.md §2.2), and this validator is what
        pays for that choice: it makes the invalid pair unrepresentable.

        ⚠️ **Load-bearing — do not delete as redundant.** It runs on read as
        well as write (``mode="after"`` fires for ``from_dynamo_item`` too), so
        a hand-written DynamoDB item cannot produce the invalid pair either.
        It retires only when the ``toolEnablement`` enum lands (§10.3).
        """
        if self.always_on and not self.enabled_by_default:
            self.enabled_by_default = True
        return self

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
            # EntityTypeIndex — lets "list every tool" be a Query on one
            # partition instead of a Scan of a table shared with roles, skills
            # and a preferences row per user. Sparse: a row without these two
            # attributes simply is not in the index, which is why existing rows
            # need backfill_tool_catalog_index.py.
            "GSI5PK": ENTITY_TYPE_TOOL,
            "GSI5SK": f"TOOL#{self.tool_id}",
            "toolId": self.tool_id,
            "displayName": self.display_name,
            "description": self.description,
            "category": self.category if isinstance(self.category, str) else self.category.value,
            "protocol": self.protocol if isinstance(self.protocol, str) else self.protocol.value,
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "retirementNote": self.retirement_note,
            "retiresOn": self.retires_on,
            "requiresOauthProvider": self.requires_oauth_provider,
            "forwardAuthToken": self.forward_auth_token,
            "tokenExchangeAudience": self.token_exchange_audience,
            "isPublic": self.is_public,
            "enabledByDefault": self.enabled_by_default,
            "alwaysOn": self.always_on,
            "createdAt": to_iso(self.created_at) if self.created_at else None,
            "updatedAt": to_iso(self.updated_at) if self.updated_at else None,
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
            # Absent on every row written before retirement metadata shipped, so
            # they read back None and the tool renders exactly as it did.
            retirement_note=item.get("retirementNote"),
            retires_on=item.get("retiresOn"),
            requires_oauth_provider=item.get("requiresOauthProvider"),
            forward_auth_token=item.get("forwardAuthToken", False),
            token_exchange_audience=item.get("tokenExchangeAudience"),
            is_public=item.get("isPublic", False),
            enabled_by_default=item.get("enabledByDefault", False),
            # Absent on every row written before always-on shipped, so it reads
            # back False and the tool behaves exactly as it did.
            always_on=item.get("alwaysOn", False),
            mcp_config=mcp_config,
            a2a_config=a2a_config,
            mcp_gateway_config=mcp_gateway_config,
            created_at=from_iso(created_at) if created_at else datetime.now(timezone.utc),
            updated_at=from_iso(updated_at) if updated_at else datetime.now(timezone.utc),
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
            "updatedAt": to_iso(self.updated_at) if self.updated_at else None,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "UserToolPreference":
        """Create from DynamoDB item."""
        updated_at = item.get("updatedAt")
        return cls(
            user_id=item.get("userId", ""),
            tool_preferences=item.get("toolPreferences", {}),
            updated_at=from_iso(updated_at) if updated_at else datetime.now(timezone.utc),
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
    always_on: bool = Field(
        default=False,
        alias="alwaysOn",
        description=(
            "An admin pinned this individual tool of the server. `enabled` is "
            "forced True and the picker must render it locked."
        ),
    )

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
    # What to do instead, and when it stops working. Only ever set on a
    # non-active tool; the SPA shows them beside the `retiring` badge so the
    # answer to "then what?" is on the same card as the bad news.
    retirement_note: Optional[str] = Field(None, alias="retirementNote")
    retires_on: Optional[str] = Field(None, alias="retiresOn")
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
    always_on: bool = Field(
        default=False,
        alias="alwaysOn",
        description=(
            "An admin pinned this tool: it is unioned into every turn and the "
            "user cannot turn it off. `is_enabled` is forced True. Note this is "
            "the *effective* lock for THIS user — it is only set when the "
            "caller's roles actually grant the tool, because always-on enables "
            "and never grants."
        ),
    )

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
    always_on: bool = Field(default=False, alias="alwaysOn")
    description: Optional[str] = None

    model_config = {"populate_by_name": True}

    def to_model(self) -> MCPToolEntry:
        return MCPToolEntry(
            name=self.name,
            needs_approval=self.needs_approval,
            always_on=self.always_on,
            description=self.description,
        )

    @classmethod
    def from_model(cls, entry: MCPToolEntry) -> "MCPToolEntryPayload":
        return cls(
            name=entry.name,
            needs_approval=entry.needs_approval,
            always_on=entry.always_on,
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
    retirement_note: Optional[str] = Field(None, max_length=300, alias="retirementNote")
    retires_on: Optional[str] = Field(None, alias="retiresOn")
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: bool = Field(default=False, alias="forwardAuthToken")
    token_exchange_audience: Optional[str] = Field(None, alias="tokenExchangeAudience")
    is_public: bool = Field(default=False, alias="isPublic")
    enabled_by_default: bool = Field(default=False, alias="enabledByDefault")
    always_on: bool = Field(default=False, alias="alwaysOn")

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
    # Same partial-update contract as `always_on` below: absent means "leave it
    # alone", and the admin form posts an explicit "" to clear one (normalised
    # to None by ToolDefinition's validator).
    retirement_note: Optional[str] = Field(None, max_length=300, alias="retirementNote")
    retires_on: Optional[str] = Field(None, alias="retiresOn")
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: Optional[bool] = Field(None, alias="forwardAuthToken")
    token_exchange_audience: Optional[str] = Field(None, alias="tokenExchangeAudience")
    is_public: Optional[bool] = Field(None, alias="isPublic")
    enabled_by_default: Optional[bool] = Field(None, alias="enabledByDefault")
    # ⚠️ Optional with no default value on the wire: the update route dumps with
    # `model_dump(exclude_unset=True)`, so an older admin client that never
    # sends this field leaves the stored value alone rather than clearing it.
    # That partial-update semantic is load-bearing for backward compatibility
    # (docs/specs/admin-always-on-tools.md §10.1) — do not "simplify" it.
    always_on: Optional[bool] = Field(None, alias="alwaysOn")

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
    retirement_note: Optional[str] = Field(None, alias="retirementNote")
    retires_on: Optional[str] = Field(None, alias="retiresOn")
    requires_oauth_provider: Optional[str] = Field(None, alias="requiresOauthProvider")
    forward_auth_token: bool = Field(default=False, alias="forwardAuthToken")
    token_exchange_audience: Optional[str] = Field(None, alias="tokenExchangeAudience")
    is_public: bool = Field(..., alias="isPublic")
    allowed_app_roles: List[str] = Field(..., alias="allowedAppRoles")
    enabled_by_default: bool = Field(..., alias="enabledByDefault")
    always_on: bool = Field(default=False, alias="alwaysOn")
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
            retirement_note=tool.retirement_note,
            retires_on=tool.retires_on,
            requires_oauth_provider=tool.requires_oauth_provider,
            forward_auth_token=tool.forward_auth_token,
            token_exchange_audience=tool.token_exchange_audience,
            is_public=tool.is_public,
            allowed_app_roles=allowed_roles or tool.allowed_app_roles,
            enabled_by_default=tool.enabled_by_default,
            always_on=tool.always_on,
            created_at=to_iso(tool.created_at) if tool.created_at else "",
            updated_at=to_iso(tool.updated_at) if tool.updated_at else "",
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
    point of discovery is to populate that list.

    `requires_oauth_provider` mirrors the catalog flag of the same name: when
    set, the route discovers an OAuth (3LO) gated server using the *admin's
    own* vaulted token for that provider (fetched via AgentCore Identity),
    injected as a bearer. This validates the admin's connection and lists the
    tools their token can see — providers like GitHub scope-filter the tool
    list to the token's grants. It fetches the admin's token only; it cannot
    mint an arbitrary end-user's token, so the discovered list reflects the
    admin's own connection. If the admin hasn't connected the provider the
    route returns 409. Mutually exclusive with `forward_auth_token`.

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
    token_exchange_audience: Optional[str] = Field(None, alias="tokenExchangeAudience")
    requires_oauth_provider: Optional[str] = Field(
        None, alias="requiresOauthProvider"
    )

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




# =============================================================================
# MCP capability snapshot
# =============================================================================
#
# An MCP server exposes three listings: tools, prompts and resources. The stack
# has only ever called ``tools/list``, so prompts and resources were invisible
# to every surface in the product.
#
# The snapshot is stored beside the catalog row (``PK=TOOL#<id>, SK=CAPABILITIES``)
# rather than inside it, on purpose. The catalog row is read on the agent build
# path; prompts and resources are of no use to the agent today, and folding a few
# KB of prompt text into an item read on every turn would be a latency and cost
# regression for a feature the agent does not consume.


# A single stored snapshot is bounded well under the 400KB DynamoDB item limit.
# Servers are free to expose hundreds of resources, and a description can be a
# whole docstring, so both the per-entry text and the entry counts are capped.
MAX_CAPABILITY_ENTRIES = 200
MAX_CAPABILITY_TEXT = 500
# Guards against a server that paginates forever.
MAX_CAPABILITY_PAGES = 20


def _clip(value: Optional[str]) -> Optional[str]:
    """Bound a single description/title so one verbose entry can't blow the item."""
    if value is None:
        return None
    text = value.strip()
    if len(text) <= MAX_CAPABILITY_TEXT:
        return text
    return text[: MAX_CAPABILITY_TEXT - 1] + "…"


class MCPPromptArgument(BaseModel):
    """One argument an MCP prompt accepts (``PromptArgument``).

    Capture originally flattened this to the name alone, which is enough to
    *describe* a prompt and not enough to *fill one in*: a form needs
    ``required`` to validate and ``description`` for the field's hint.

    ``required`` defaults to False because that is what the MCP type says —
    ``required`` is ``bool | None`` and absent means not required. A snapshot
    taken before this model existed stored bare strings; those rehydrate here
    with the same default, so an old snapshot under-constrains a form rather
    than blocking the user on a field we never actually learned about.
    """

    name: str
    description: Optional[str] = None
    required: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, data: Union[str, dict]) -> "MCPPromptArgument":
        # Pre-widening snapshots stored the name as a bare string.
        if isinstance(data, str):
            return cls(name=data)
        return cls(
            name=data.get("name", ""),
            description=data.get("description"),
            required=bool(data.get("required", False)),
        )


class MCPPromptEntry(BaseModel):
    """A prompt template exposed by an MCP server (``prompts/list``)."""

    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    arguments: List[MCPPromptArgument] = Field(
        default_factory=list,
        description="Arguments the prompt accepts, in the order the server listed them.",
    )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": [a.to_dict() for a in self.arguments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPPromptEntry":
        return cls(
            name=data.get("name", ""),
            title=data.get("title"),
            description=data.get("description"),
            arguments=[
                MCPPromptArgument.from_dict(a) for a in (data.get("arguments") or [])
            ],
        )


class MCPResourceEntry(BaseModel):
    """A resource exposed by an MCP server (``resources/list``).

    ``uri_template`` is set for entries that came from
    ``resources/templates/list`` — those are patterns such as
    ``canvas://courses/{course_id}/syllabus`` rather than concrete URIs, and a
    caller has to fill the placeholders before reading one.
    """

    uri: str
    name: Optional[str] = None
    description: Optional[str] = None
    mime_type: Optional[str] = Field(None, alias="mimeType")
    uri_template: bool = Field(default=False, alias="uriTemplate")

    model_config = {"populate_by_name": True}

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
            "uriTemplate": self.uri_template,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCPResourceEntry":
        return cls(
            uri=data.get("uri", ""),
            name=data.get("name"),
            description=data.get("description"),
            mime_type=data.get("mimeType"),
            uri_template=bool(data.get("uriTemplate", False)),
        )


class ToolCapabilitySnapshot(BaseModel):
    """What one MCP server told us it offers, and when it said so.

    Persisted so a detail view never has to open a live MCP session to render.
    A 31-card catalogue opening one session per server would be unusable, and
    OAuth-gated servers cannot be reached at all without a consent token.

    ``supports_prompts`` / ``supports_resources`` record whether the server
    answered the listing at all. A server that does not implement prompts
    returns a JSON-RPC "method not found", which is a different fact from a
    server that implements prompts and has none — and the UI should say
    different things about each.
    """

    tool_id: str = Field(..., alias="toolId")
    prompts: List[MCPPromptEntry] = Field(default_factory=list)
    resources: List[MCPResourceEntry] = Field(default_factory=list)
    supports_prompts: bool = Field(default=False, alias="supportsPrompts")
    supports_resources: bool = Field(default=False, alias="supportsResources")
    discovered_at: Optional[str] = Field(None, alias="discoveredAt")
    discovered_by: Optional[str] = Field(None, alias="discoveredBy")
    #: Set when the last attempt failed, so the UI can distinguish "this server
    #: offers nothing" from "we could not ask".
    error: Optional[str] = None
    #: True when a listing was cut short by the entry cap above.
    truncated: bool = Field(default=False)

    model_config = {"populate_by_name": True}

    def to_dynamo_item(self) -> dict:
        return {
            "PK": f"TOOL#{self.tool_id}",
            "SK": "CAPABILITIES",
            "toolId": self.tool_id,
            "prompts": [p.to_dict() for p in self.prompts],
            "resources": [r.to_dict() for r in self.resources],
            "supportsPrompts": self.supports_prompts,
            "supportsResources": self.supports_resources,
            "discoveredAt": self.discovered_at,
            "discoveredBy": self.discovered_by,
            "error": self.error,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dynamo_item(cls, item: dict) -> "ToolCapabilitySnapshot":
        return cls(
            tool_id=item.get("toolId", ""),
            prompts=[MCPPromptEntry.from_dict(p) for p in item.get("prompts") or []],
            resources=[
                MCPResourceEntry.from_dict(r) for r in item.get("resources") or []
            ],
            supports_prompts=bool(item.get("supportsPrompts", False)),
            supports_resources=bool(item.get("supportsResources", False)),
            discovered_at=item.get("discoveredAt"),
            discovered_by=item.get("discoveredBy"),
            error=item.get("error"),
            truncated=bool(item.get("truncated", False)),
        )


# =============================================================================
# Resolved prompt (prompts/get)
# =============================================================================
#
# Unlike the capability snapshot, a resolved prompt is never persisted. It is
# composed from arguments the user just typed, it can be large, and it is of no
# use to anyone but the person who asked for it — storing it would be a cost
# with no reader.

#: A resolved prompt is shown to a person, so it is bounded by what a person
#: will actually read rather than by the DynamoDB item limit.
MAX_RESOLVED_PROMPT_CHARS = 20000
MAX_RESOLVED_PROMPT_MESSAGES = 20


class ResolvedPromptMessage(BaseModel):
    """One message a server composed for a prompt.

    ``kind`` is the MCP content type. Anything other than ``text`` carries no
    body — see ``_message_text`` — and the UI says so rather than rendering an
    empty message.
    """

    role: str
    kind: str = "text"
    text: str = ""


class ResolvedPrompt(BaseModel):
    """The result of ``prompts/get`` for one prompt."""

    description: Optional[str] = None
    messages: List[ResolvedPromptMessage] = Field(default_factory=list)
    #: True when the message list or its text was cut short by the caps above.
    truncated: bool = Field(default=False)
