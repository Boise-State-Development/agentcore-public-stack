import { ToolResultContent } from '../../../../services/models/message.model';

/**
 * Represents a single tool call for display in the inline rail.
 * Populated from ToolUseData on the existing ContentBlock.
 */
export interface ToolCallDisplay {
  /** Unique ID (from toolUseData.toolUseId) */
  id: string;

  /** MCP tool name (from toolUseData.name) */
  toolName: string;

  /** The input arguments sent to the tool (from toolUseData.input) */
  input: Record<string, unknown>;

  /** The raw result (from toolUseData.result) -- kept as-is for rendering */
  result?: {
    status: string;
    content: ToolResultContent[];
  };

  /** Execution status (from toolUseData.status, defaults to 'pending').
   *  ``awaiting_auth`` is derived in the message renderer when the tool was
   *  paused on an OAuth consent gate — the tool didn't fail, it's waiting
   *  for the user to authorize. */
  status: 'pending' | 'complete' | 'error' | 'awaiting_auth';

  /**
   * Partially-generated long output (e.g. an artifact's HTML) decoded from the
   * still-incomplete tool-call JSON while the model is streaming it. Present
   * only while the call is in flight; cleared once a result arrives. Used to
   * show live "generating output" feedback in the rail.
   */
  streamingContent?: string;

  /** Optional LLM-generated one-line summary of this tool call's result */
  summary?: string;

  /** Execution duration in milliseconds (if tracked -- future enhancement) */
  durationMs?: number;
}

/**
 * One backend tool batch inside a rail: the calls the agent issued together,
 * and the model-generated line describing what that round accomplished.
 *
 * A rail's grouping is a CLIENT-side decision — consecutive tool calls across
 * the messages of one assistant run — and deliberately need not line up with
 * the backend's batches, which are one per event-loop cycle. So a rail that
 * collapses a four-step pipeline contains four batches, each with its own
 * summary. Rendering only the first one made the rail claim the whole group
 * did what its opening round did.
 */
export interface ToolCallBatch {
  /** Stable key for rendering: the batch id, or the first call's id. */
  key: string;

  /**
   * Model-generated line for this batch, when the tool-summary side-channel
   * produced one. Absent for an unsummarized round (flag off, Nova failed,
   * or a conversation older than the feature), which falls back to the
   * per-call descriptions below it.
   */
  summary?: string;

  /** The calls issued in this batch, in order. */
  calls: ToolCallDisplay[];
}

/**
 * A group of consecutive tool calls displayed as a single inline rail.
 */
export interface ToolCallGroup {
  /** All tool calls in this consecutive sequence, flattened across batches. */
  calls: ToolCallDisplay[];

  /**
   * The calls segmented by the backend batch that produced them, in order.
   * Optional: a caller that doesn't know about batches (older callers, tests)
   * omits it and the rail treats the whole group as one unsummarized batch.
   */
  batches?: ToolCallBatch[];

  /**
   * Explicit override for the collapsed header line. Left unset by the live
   * path, which derives the header from `batches` — this exists so a caller
   * can state the header outright.
   */
  groupSummary?: string;
}
