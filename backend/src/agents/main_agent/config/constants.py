"""
Centralized constants for the main_agent package.

All environment variable names, default values, and shared string constants
live here. This eliminates magic strings scattered across modules and provides
a single reference for configuration.

Usage:
    from agents.main_agent.config.constants import EnvVars, Defaults, Prefixes
"""


class EnvVars:
    """Environment variable names used across the main_agent package.

    Organized by subsystem. All values are strings (the env var name, not the value).
    """

    # --- AgentCore Memory ---
    MEMORY_ID = "AGENTCORE_MEMORY_ID"
    AWS_REGION = "AWS_REGION"
    MEMORY_RELEVANCE_SCORE = "AGENTCORE_MEMORY_RELEVANCE_SCORE"
    MEMORY_TOP_K = "AGENTCORE_MEMORY_TOP_K"

    # --- Compaction ---
    COMPACTION_ENABLED = "AGENTCORE_MEMORY_COMPACTION_ENABLED"
    COMPACTION_TOKEN_THRESHOLD = "AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD"
    COMPACTION_PROTECTED_TURNS = "AGENTCORE_MEMORY_COMPACTION_PROTECTED_TURNS"
    COMPACTION_MAX_TOOL_CONTENT_LENGTH = "AGENTCORE_MEMORY_COMPACTION_MAX_TOOL_CONTENT_LENGTH"

    # --- DynamoDB Tables ---
    DYNAMODB_SESSIONS_METADATA_TABLE = "DYNAMODB_SESSIONS_METADATA_TABLE_NAME"
    DYNAMODB_QUOTA_TABLE = "DYNAMODB_QUOTA_TABLE"
    DYNAMODB_QUOTA_EVENTS_TABLE = "DYNAMODB_QUOTA_EVENTS_TABLE"

    # --- Retry Configuration ---
    RETRY_BOTO_MAX_ATTEMPTS = "RETRY_BOTO_MAX_ATTEMPTS"
    RETRY_BOTO_MODE = "RETRY_BOTO_MODE"
    RETRY_CONNECT_TIMEOUT = "RETRY_CONNECT_TIMEOUT"
    RETRY_READ_TIMEOUT = "RETRY_READ_TIMEOUT"
    RETRY_SDK_MAX_ATTEMPTS = "RETRY_SDK_MAX_ATTEMPTS"
    RETRY_SDK_INITIAL_DELAY = "RETRY_SDK_INITIAL_DELAY"
    RETRY_SDK_MAX_DELAY = "RETRY_SDK_MAX_DELAY"

    # --- API Keys ---
    OPENAI_API_KEY = "OPENAI_API_KEY"
    GOOGLE_GEMINI_API_KEY = "GOOGLE_GEMINI_API_KEY"

    # --- Gateway ---
    GATEWAY_MCP_ENABLED = "AGENTCORE_GATEWAY_MCP_ENABLED"

    # --- Frontend ---
    FRONTEND_URL = "FRONTEND_URL"

    # --- Runtime Context (written by StreamCoordinator) ---
    SESSION_ID = "SESSION_ID"
    USER_ID = "USER_ID"

    # --- Voice Agent ---
    NOVA_SONIC_MODEL_ID = "NOVA_SONIC_MODEL_ID"
    NOVA_SONIC_VOICE = "NOVA_SONIC_VOICE"
    NOVA_SONIC_MAX_MESSAGES = "NOVA_SONIC_MAX_MESSAGES"


class Defaults:
    """Default values for configuration parameters.

    Organized to match the EnvVars they pair with.
    """

    # --- Model ---
    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    TEMPERATURE = 0.7
    CACHING_ENABLED = True

    # --- AWS ---
    AWS_REGION = "us-west-2"

    # --- Memory Retrieval ---
    MEMORY_RELEVANCE_SCORE = 0.7
    MEMORY_TOP_K = 10

    # --- Compaction ---
    COMPACTION_ENABLED = True
    COMPACTION_TOKEN_THRESHOLD = 100_000
    COMPACTION_PROTECTED_TURNS = 3
    COMPACTION_MAX_TOOL_CONTENT_LENGTH = 500

    # --- DynamoDB Tables ---
    DYNAMODB_QUOTA_TABLE = "UserQuotas"
    DYNAMODB_QUOTA_EVENTS_TABLE = "QuotaEvents"

    # --- Retry ---
    RETRY_BOTO_MAX_ATTEMPTS = 3
    RETRY_BOTO_MODE = "standard"
    RETRY_CONNECT_TIMEOUT = 5
    RETRY_READ_TIMEOUT = 120
    RETRY_SDK_MAX_ATTEMPTS = 4
    RETRY_SDK_INITIAL_DELAY = 2.0
    RETRY_SDK_MAX_DELAY = 16.0

    # --- Frontend ---
    FRONTEND_URL = "http://localhost:4200"

    # --- Gateway ---
    GATEWAY_MCP_ENABLED = True

    # --- Voice Agent ---
    NOVA_SONIC_MODEL_ID = "amazon.nova-2-sonic-v1:0"
    NOVA_SONIC_VOICE = "tiffany"
    NOVA_SONIC_INPUT_RATE = 16000
    NOVA_SONIC_OUTPUT_RATE = 16000
    NOVA_SONIC_MAX_MESSAGES = 20
    VOICE_AGENT_ID = "voice"


class Prefixes:
    """String prefixes used for tool ID classification and session routing."""

    GATEWAY_TOOL = "gateway_"
    PREVIEW_SESSION = "preview-"
