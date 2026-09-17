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
    COMPACTION_CACHE_TTL_SECONDS = "AGENTCORE_MEMORY_COMPACTION_CACHE_TTL_SECONDS"
    # Model-relative thresholds (docs/specs/compaction-model-relative-thresholds.md).
    # The kill switch reverts to the fixed TOKEN_THRESHOLD and the legacy
    # turn-count cut; the ratios/caps shape the per-model ceiling and floor.
    COMPACTION_MODEL_RELATIVE_ENABLED = "AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED"
    COMPACTION_CEILING_RATIO = "AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO"
    COMPACTION_CEILING_CAP_TOKENS = "AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS"
    COMPACTION_FLOOR_RATIO = "AGENTCORE_MEMORY_COMPACTION_FLOOR_RATIO"
    COMPACTION_HARD_CEILING_RATIO = "AGENTCORE_MEMORY_COMPACTION_HARD_CEILING_RATIO"
    COMPACTION_HARD_CEILING_MULTIPLIER = "AGENTCORE_MEMORY_COMPACTION_HARD_CEILING_MULTIPLIER"
    # Strands conversation-manager window (messages). Our compaction owns
    # history size; the SDK's default 40-message SlidingWindowConversationManager
    # would otherwise slide the front of the list every turn past 40 messages
    # (a prefix re-write per turn, and it moves the coordinates the compaction
    # checkpoint is expressed in). Setting this to 40 restores the SDK default.
    CONVERSATION_WINDOW_MESSAGES = "AGENTCORE_CONVERSATION_WINDOW_MESSAGES"
    # Bounded compaction summary (spiral spec PR-2 /
    # compaction-model-relative-thresholds.md §3.6). Budget in tokens; the
    # re-summarize call is a Nova Micro side-channel with its own kill switch.
    COMPACTION_SUMMARY_TOKEN_BUDGET = "AGENTCORE_MEMORY_COMPACTION_SUMMARY_TOKEN_BUDGET"
    COMPACTION_SUMMARY_MODEL_ENABLED = "AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ENABLED"
    COMPACTION_SUMMARY_MODEL_ID = "AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID"
    # Paid-when-free scheduling (thresholds spec §3.5): a cut is computed and
    # persisted as PENDING post-turn and applied to the live list pre-call only
    # when the prefix re-write is free (cache expired, model/agent switched)
    # or unavoidable (hard ceiling). "false" applies cuts immediately (PR-1/2).
    COMPACTION_DEFERRED_APPLY_ENABLED = "AGENTCORE_MEMORY_COMPACTION_DEFERRED_APPLY_ENABLED"
    # Tool-result offload at intake (thresholds spec §3.6 / PR-4): oversized
    # tool results are stored in S3 (user-files bucket, per-session prefix) and
    # replaced in context by a bounded preview + retrieval references before
    # they ever enter the cacheable prefix. Strands' vended ContextOffloader.
    TOOL_RESULT_OFFLOAD_ENABLED = "AGENTCORE_TOOL_RESULT_OFFLOAD_ENABLED"
    TOOL_RESULT_OFFLOAD_MAX_TOKENS = "AGENTCORE_TOOL_RESULT_OFFLOAD_MAX_TOKENS"
    TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS = "AGENTCORE_TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS"
    # Selective long cache TTL on the STATIC prefix (thresholds spec §3.6,
    # PR-5). "1h" puts a 1-hour TTL on the tools and system cachePoints only;
    # the message-level point stays at Bedrock's 5-minute default. Unset/empty
    # = today's shape. An experiment arm: default OFF until the live probe
    # (scripts/probe_static_prefix_ttl.py) and the cost rows say it pays.
    PROMPT_CACHE_STATIC_PREFIX_TTL = "AGENTCORE_PROMPT_CACHE_STATIC_PREFIX_TTL"

    # --- Restored-history repair ---
    # Kill switch for the restore-time tool-pairing/alternation repair
    # (`TurnBasedSessionManager._repair_tool_pairing`). Default ON; set to
    # "false" to disable. See the method docstring for the failure it guards.
    HISTORY_REPAIR_ENABLED = "AGENTCORE_MEMORY_HISTORY_REPAIR_ENABLED"

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
    # Kill switch for retrying Bedrock's transient pre-stream faults
    # (ServiceUnavailableException et al). Default on; set "false" to fall
    # back to Strands' stock throttling-only retry.
    RETRY_TRANSIENT_SERVICE_ERRORS = "RETRY_TRANSIENT_SERVICE_ERRORS"

    # --- API Keys ---
    OPENAI_API_KEY = "OPENAI_API_KEY"
    GOOGLE_GEMINI_API_KEY = "GOOGLE_GEMINI_API_KEY"

    # --- Gateway ---
    GATEWAY_MCP_ENABLED = "AGENTCORE_GATEWAY_MCP_ENABLED"
    # Gateway inbound authorizer mode: "iam" (default) or "jwt".
    # MUST match the deployed Gateway's authorizerType. CDK sets this from
    # `config.gateway.inboundAuth` so the two cannot drift; the default is
    # deliberately the conservative one (see Defaults.GATEWAY_INBOUND_AUTH).
    # With "iam" the agent SigV4-signs Gateway calls; with "jwt" it sends the
    # signed-in user's Cognito access token as a Bearer token.
    GATEWAY_INBOUND_AUTH = "AGENTCORE_GATEWAY_INBOUND_AUTH"

    # --- MCP Apps (host renderer initiative) ---
    MCP_APPS_HOST_ENABLED = "AGENTCORE_MCP_APPS_HOST_ENABLED"
    # Origin of the sandbox-proxy (proxy.html) the SPA frames an MCP App in.
    # Deploy pipeline sources it from SSM /{projectPrefix}/mcp-sandbox/origin
    # (published by the PR #1 CDK stack). Surfaced to the SPA on the
    # `ui_resource` SSE event so the frontend needs no separate config fetch.
    MCP_APPS_SANDBOX_ORIGIN = "AGENTCORE_MCP_APPS_SANDBOX_ORIGIN"

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
    # Bedrock prompt-cache TTL (seconds); see CompactionConfig.cache_ttl_seconds
    COMPACTION_CACHE_TTL_SECONDS = 300
    # Model-relative compaction policy — see
    # docs/specs/compaction-model-relative-thresholds.md §3.1 for the table
    # these produce. ceiling = min(window * CEILING_RATIO, CEILING_CAP_TOKENS);
    # floor = ceiling * FLOOR_RATIO; hard = min(window * HARD_CEILING_RATIO,
    # ceiling * HARD_CEILING_MULTIPLIER). COMPACTION_TOKEN_THRESHOLD above is
    # the ceiling used when the model's window is unknown.
    COMPACTION_MODEL_RELATIVE_ENABLED = True
    COMPACTION_CEILING_RATIO = 0.5
    # 100k, not 200k: the 2026-09-15 replay of 20 heavy Sonnet 5 sessions
    # priced a 200k/50k policy 43% above 100k/25k on the input side, because
    # 36% of cache-write dollars are cold re-writes after a >5 min pause and
    # their size is the context at the pause. Raise only on evidence from
    # compaction_forced + the cost anatomy (spec §3.1).
    COMPACTION_CEILING_CAP_TOKENS = 100_000
    COMPACTION_FLOOR_RATIO = 0.25
    COMPACTION_HARD_CEILING_RATIO = 0.7
    COMPACTION_HARD_CEILING_MULTIPLIER = 1.5
    # Effectively "never trim proactively" — compaction decides what leaves the
    # prompt. Overflow recovery (reduce_context on ContextWindowOverflow) still
    # works at any window size.
    CONVERSATION_WINDOW_MESSAGES = 2000
    # 8k tokens ≈ 32k chars: a third of the 25k floor, so a bounded summary
    # can never by itself hold a session above the ceiling (the incident's
    # summary was 40k tokens against a 100k threshold). Same figure the admin
    # SUMMARY_OVER_BUDGET diagnosis reads.
    COMPACTION_SUMMARY_TOKEN_BUDGET = 8_000
    COMPACTION_SUMMARY_MODEL_ENABLED = True
    # Same cheap model as the title and tool-batch side-channels.
    COMPACTION_SUMMARY_MODEL_ID = "us.amazon.nova-micro-v1:0"
    COMPACTION_DEFERRED_APPLY_ENABLED = True
    # Tool-result offload gate. 4k is well under the 25k compaction floor, so a
    # protected tail of a few big results can no longer hold a session above
    # the ceiling on its own; the 1k preview keeps the part of a result models
    # actually quote (headers, first rows, the first error).
    TOOL_RESULT_OFFLOAD_ENABLED = True
    TOOL_RESULT_OFFLOAD_MAX_TOKENS = 4_000
    TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS = 1_000
    TOOL_RESULT_OFFLOAD_S3_PREFIX = "compaction-offload"
    # Default OFF, deliberately against the flags-default-on house style: a
    # caching default adopted on inspection alone has already shipped wrong
    # once (#954 measured 57% more expensive live before #956 reverted it).
    # The 1h write premium is 2x base vs 1.25x at 5m, so this is a bet on the
    # gap distribution that has to be measured, not read off the source.
    PROMPT_CACHE_STATIC_PREFIX_TTL = ""

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
    RETRY_TRANSIENT_SERVICE_ERRORS = True

    # --- Frontend ---
    FRONTEND_URL = "http://localhost:4200"

    # --- Gateway ---
    GATEWAY_MCP_ENABLED = True
    # Fail-safe default: SigV4. An absent env var must mean "behave like the
    # Gateway that is actually deployed today", and AgentCore refuses to change
    # a Gateway's authorizerType after creation ("Authorizer type cannot be
    # updated for an existing gateway"), so every existing Gateway is AWS_IAM.
    # Defaulting to 'jwt' made a partial deploy (infra rolled back, backend
    # shipped) send bearer tokens to an IAM Gateway and 401 every tool call.
    # CDK sets this explicitly from `config.gateway.inboundAuth`, so a JWT
    # Gateway still gets 'jwt' — but only once the infra says so.
    GATEWAY_INBOUND_AUTH = "iam"

    # --- MCP Apps (host renderer initiative) ---
    # Gates the entire MCP Apps host surface. Flipped on in PR #7 of
    # docs/kaizen/scoping/mcp-apps-host-renderer.md (the full #1–#6 chain
    # landed first). Set AGENTCORE_MCP_APPS_HOST_ENABLED=false to opt a
    # given environment back out.
    MCP_APPS_HOST_ENABLED = True
    # Empty unless the mcp-sandbox stack is deployed: the inference-api CDK
    # stack wires `/{prefix}/mcp-sandbox/origin` into this env only when
    # `config.mcpSandbox.enabled` (conditional-SSM, mirrors artifacts).
    # An empty origin keeps the surface dormant — the SPA has no proxy
    # origin to frame an App in even with the host flag on.
    MCP_APPS_SANDBOX_ORIGIN = ""

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
