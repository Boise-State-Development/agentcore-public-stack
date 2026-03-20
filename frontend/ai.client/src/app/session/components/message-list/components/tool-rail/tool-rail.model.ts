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

  /** Execution status (from toolUseData.status, defaults to 'pending') */
  status: 'pending' | 'complete' | 'error';

  /** Optional LLM-generated one-line summary of this tool call's result */
  summary?: string;

  /** Execution duration in milliseconds (if tracked -- future enhancement) */
  durationMs?: number;
}

/**
 * A group of consecutive tool calls displayed as a single inline rail.
 */
export interface ToolCallGroup {
  /** All tool calls in this consecutive sequence */
  calls: ToolCallDisplay[];

  /**
   * Optional LLM-generated summary of the entire group.
   * When present, displayed as the collapsed header text.
   * When absent, the component falls back to chaining tool names.
   */
  groupSummary?: string;
}
