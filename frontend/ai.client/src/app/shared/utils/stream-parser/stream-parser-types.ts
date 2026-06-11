/**
 * Stream Parser Types
 *
 * Shared type definitions for SSE stream parsing used by both the main
 * StreamParserService and the PreviewChatService.
 */

import type {
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
} from '../../../session/services/models/message.model';

import type { MetadataEvent } from '../../../session/services/models/content-types';

// Re-export for convenience
export type {
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
  MetadataEvent,
};

/**
 * Quota warning event from the stream
 */
export interface QuotaWarningEvent {
  type: 'quota_warning';
  warningLevel: string;
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  remaining: number;
  message: string;
}

/**
 * Quota exceeded event from the stream
 */
export interface QuotaExceededEvent {
  type: 'quota_exceeded';
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  periodType: string;
  tierName?: string;
  resetInfo: string;
  message: string;
}

/**
 * Stream error event (structured error from backend)
 */
export interface StreamErrorEvent {
  error: string;
  code: string;
  detail?: string;
  recoverable: boolean;
  metadata?: Record<string, unknown>;
}

/**
 * Conversational stream error (displayed as assistant message)
 */
export interface ConversationalStreamErrorEvent {
  type: 'stream_error';
  code: string;
  message: string;
  recoverable: boolean;
  retry_after?: number;
  metadata?: Record<string, unknown>;
}

/**
 * Reasoning event containing chain-of-thought text
 */
export interface ReasoningEvent {
  reasoningText?: string;
}

/**
 * OAuth required event — emitted when an external MCP tool needs the user
 * to grant consent via AgentCore Identity. The agent's tool call is paused
 * (Strands interrupt) and the frontend resumes the same turn after the
 * user completes consent by POSTing back the carried `interruptId`.
 */
export interface OAuthRequiredEvent {
  type: 'oauth_required';
  providerId: string;
  authorizationUrl: string;
  interruptId: string;
}

/**
 * Tool approval required event — emitted when an MCP tool flagged
 * `needs_approval` in the catalog is about to run. The agent's tool call
 * is paused (Strands interrupt); the frontend renders an inline
 * approve/decline prompt and resumes the same turn by POSTing the carried
 * `interruptId` with `response: "approved" | "declined"`.
 */
export interface ToolApprovalRequiredEvent {
  type: 'tool_approval_required';
  interruptId: string;
  toolUseId: string;
  toolName: string;
  /** JSON-encoded tool input arguments. Pre-stringified by the backend so
   *  one shape works for both the live SSE event and the persisted
   *  PendingInterrupt breadcrumb (which DynamoDB would otherwise coerce
   *  floats inside). */
  toolInput?: string;
  message: string;
}

/**
 * Compaction event — emitted after the final `metadata` event (so the badge
 * updates first) and before `done` when the backend rolls older turns into
 * a summary on this turn. The frontend feeds it to `CompactionSummaryService`,
 * which increments a running total and renders a single end-of-conversation
 * "Earlier messages summarized" indicator. (An earlier draft of this work
 * placed inline dividers anchored at `newCheckpoint`; that variant was
 * dropped because the mid-conversation drop-in caused jarring layout shifts.)
 *
 * `summarizedTurns` is the *delta* count of turns rolled up at this
 * compaction event, not the cumulative total across prior compactions —
 * the service sums these deltas to keep its own running total, which is
 * also persisted on the backend as `totalSummarizedTurns` for refresh
 * survival. `previousCheckpoint` / `newCheckpoint` are kept on the wire
 * for diagnostics and possible future per-event UI.
 */
export interface CompactionEvent {
  type: 'compaction';
  previousCheckpoint: number;
  newCheckpoint: number;
  summarizedTurns: number;
  inputTokens: number;
}

/**
 * Artifact event — emitted once per artifact created or updated during a
 * turn, after the final `metadata`/`compaction` events and before `done`
 * (same post-`message_stop` side-channel placement as `oauth_required`).
 *
 * The artifact's HTML content is never carried on the wire: it lives in
 * S3 and renders in a sandboxed iframe via the artifact render origin.
 * This event only signals existence so the SPA can show an inline card
 * and open the panel (which mints a short-lived render token on demand).
 *
 * `action` is `created` for v1, `updated` for any later version. Cards
 * also hydrate on session load via the app-api list endpoint; the SPA
 * dedupes by `artifactId` keeping the highest `version`.
 *
 * `producedByMessageIndex` is the 0-based index of the turn's final
 * assistant message (`msg-{sessionId}-{index}`), stamped by the stream
 * coordinator so the SPA can anchor the card inline after that message.
 * Null when the index couldn't be resolved — the SPA falls back to the
 * end-of-conversation strip.
 */
export interface ArtifactEvent {
  type: 'artifact';
  artifactId: string;
  version: number;
  title: string;
  contentType: string;
  sessionId: string;
  updatedAt: string;
  action: 'created' | 'updated';
  producedByMessageIndex?: number | null;
}

/**
 * CSP domain allowlists declared by an MCP App resource (SEP-1865
 * `McpUiResourceCsp`). The sandbox proxy composes the inner iframe's CSP
 * from these plus the spec's deny-by-default fallbacks.
 */
export interface McpUiCsp {
  connectDomains?: string[];
  resourceDomains?: string[];
  frameDomains?: string[];
  baseUriDomains?: string[];
}

/**
 * Sandbox permissions an MCP App resource requested (SEP-1865). Each key,
 * when present (as an empty object), maps to a Permissions-Policy feature on
 * the inner iframe's `allow` attribute. Absence = not requested.
 */
export interface McpUiPermissions {
  camera?: Record<string, never>;
  microphone?: Record<string, never>;
  geolocation?: Record<string, never>;
  clipboardWrite?: Record<string, never>;
}

/**
 * UI resource event — emitted by the backend (PR #3) right after the
 * correlated `tool_result` when the tool declared a `ui://` MCP App
 * resource (SEP-1865). Unlike `artifact`/`oauth_required` this is an
 * INLINE event during streaming, correlated to its tool-use block by
 * `toolUseId`. The HTML is fetched server-side via `resources/read` and
 * inlined here so the frontend needs no MCP client of its own.
 *
 * `sandboxOrigin` is the origin of the deployed sandbox-proxy (proxy.html)
 * the SPA frames the App in; empty until that stack is deployed + wired
 * (the whole surface is inert behind the backend host flag until then).
 *
 * The entire MCP Apps surface stays dark until PR #7 flips the backend
 * `AGENTCORE_MCP_APPS_HOST_ENABLED` flag, so in practice this event does
 * not arrive in production yet.
 */
export interface UiResourceEvent {
  type: 'ui_resource';
  toolUseId: string;
  resourceUri: string;
  html: string;
  mimeType: string;
  csp: McpUiCsp;
  permissions: McpUiPermissions;
  sandboxOrigin: string;
  /**
   * Server display name for the App header (SEP-1865 Claude parity), e.g.
   * "Excalidraw". Resolved backend-side from the MCP `serverInfo.title`/`name`,
   * falling back to the `ui://` authority. Optional: absent on resources
   * persisted before this field shipped (the frame then derives it from
   * `resourceUri`).
   */
  serverName?: string;
  /**
   * Server icon `src` for the App header — an http(s) or `data:` URL from the
   * server's advertised `serverInfo.icons`. Empty/absent when the server
   * declared none; the frame then renders a generic glyph. Rendered in the
   * SPA header `<img>` (not the sandboxed iframe), with a glyph fallback on
   * load error.
   */
  icon?: string;
  /**
   * Agent-facing tool name (e.g. `create_view`) for the App header. Carried on
   * the event so the frame's header shows the name + running shimmer the
   * instant the frame promotes — the resource is recorded atomically with the
   * promotion, whereas the streamed message content (the frame's `toolName`
   * input) can land on a separate tick. Optional: absent on resources persisted
   * before this field shipped (the frame falls back to the input).
   */
  toolName?: string;
}

/**
 * Tool-input-partial event — emitted by the backend (SEP-1865
 * `ui/notifications/tool-input-partial`) repeatedly while the model is still
 * STREAMING a UI tool's arguments, after the App frame has been mounted early
 * (at the tool's `content_block_start`). `arguments` is the streamed prefix of
 * the tool input, server-side "healed" into a valid object. The frame relays
 * each one to the App so a progressively-rendering App (e.g. Excalidraw's
 * guided camera tour) animates as arguments arrive, then receives the complete
 * `tool-input` once streaming finishes. Correlated to its tool-use block by
 * `toolUseId`. Inert behind the backend host flag, like `ui_resource`.
 */
export interface ToolInputPartialEvent {
  type: 'ui_tool_input_partial';
  toolUseId: string;
  arguments: Record<string, unknown>;
}

/**
 * Tool result event data structure
 */
export interface ToolResultEventData {
  tool_result: {
    toolUseId: string;
    content?: Array<{
      text?: string;
      json?: unknown;
      image?: {
        format?: string;
        source?: { data?: string; bytes?: string };
        data?: string;
      };
    }>;
    status?: 'success' | 'error';
  };
}

/**
 * All supported SSE event types
 */
export type StreamEventType =
  | 'message_start'
  | 'content_block_start'
  | 'content_block_delta'
  | 'content_block_stop'
  | 'tool_use'
  | 'tool_result'
  | 'message_stop'
  | 'done'
  | 'error'
  | 'metadata'
  | 'reasoning'
  | 'quota_warning'
  | 'quota_exceeded'
  | 'stream_error'
  | 'citation'
  | 'oauth_required'
  | 'compaction'
  | 'artifact'
  | 'ui_resource'
  | 'ui_tool_input_partial';

/**
 * Union type of all possible event data types
 */
export type StreamEventData =
  | MessageStartEvent
  | ContentBlockStartEvent
  | ContentBlockDeltaEvent
  | ContentBlockStopEvent
  | MessageStopEvent
  | ToolUseEvent
  | ToolResultEventData
  | MetadataEvent
  | ReasoningEvent
  | QuotaWarningEvent
  | QuotaExceededEvent
  | StreamErrorEvent
  | ConversationalStreamErrorEvent
  | Citation
  | OAuthRequiredEvent
  | CompactionEvent
  | ArtifactEvent
  | UiResourceEvent
  | ToolInputPartialEvent
  | null
  | undefined;

/**
 * Parsed stream event with type and data
 */
export interface ParsedStreamEvent {
  type: StreamEventType;
  data: StreamEventData;
}

/**
 * Content block builder type (text or tool_use)
 */
export type ContentBlockType = 'text' | 'tool_use' | 'toolUse' | 'reasoningContent';

/**
 * Tool result content structure
 */
export interface ToolResultContent {
  text?: string;
  json?: unknown;
  image?: { format: string; data: string };
  document?: Record<string, unknown>;
}

/**
 * Internal representation of a content block being built from stream events
 */
export interface ContentBlockBuilder {
  index: number;
  type: ContentBlockType;
  textChunks: string[];
  inputChunks: string[];
  reasoningChunks: string[];
  toolUseId?: string;
  toolName?: string;
  result?: {
    content: ToolResultContent[];
    status: 'success' | 'error';
  };
  status?: 'pending' | 'complete' | 'error';
  isComplete: boolean;
}

/**
 * Internal representation of a message being built from stream events
 */
export interface MessageBuilder {
  id: string;
  role: 'user' | 'assistant';
  contentBlocks: Map<number, ContentBlockBuilder>;
  createdAt: string;
  isComplete: boolean;
}

/**
 * Tool progress state for UI feedback
 */
export interface ToolProgress {
  visible: boolean;
  message?: string;
  toolName?: string;
  toolUseId?: string;
  startTime?: number;
}
