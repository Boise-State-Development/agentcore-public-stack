import { Message } from '../models/message.model';

/**
 * Match tool results from user messages to their corresponding tool uses in assistant messages.
 * This reconstructs the tool state as it appears during streaming.
 *
 * The backend stores tool_use and tool_result as separate content blocks in separate messages:
 * - Assistant message: contains toolUse blocks
 * - User message: contains toolResult blocks
 *
 * This method merges the results into the toolUse blocks for proper UI display.
 *
 * @param messages - Array of messages from the API
 * @returns Processed messages with tool results embedded in tool uses
 */
export function matchToolResultsToToolUses(messages: Message[]): Message[] {
  // Create a map of toolUseId -> tool result for quick lookup
  const toolResultMap = new Map<string, { content: any[]; status: 'success' | 'error' }>();

  // First pass: collect all tool results from user messages
  // Note: Tool results are now embedded in toolUse.result, but we still check
  // for legacy toolResult blocks for backwards compatibility
  for (const message of messages) {
    if (message.role === 'user') {
      for (const contentBlock of message.content) {
        // Check for legacy toolResult blocks (deprecated format)
        if (contentBlock.type === 'toolResult' && contentBlock.toolResult) {
          const toolResult = contentBlock.toolResult as any;
          const toolUseId = toolResult.toolUseId;
          if (toolUseId) {
            // Determine status from explicit status field or by inspecting content
            let status: 'success' | 'error' = toolResult.status || 'success';

            // Check if content indicates an error (for backwards compatibility with old format)
            if (status === 'success' && toolResult.content && Array.isArray(toolResult.content)) {
              for (const contentItem of toolResult.content) {
                // Check if JSON content has success: false
                if (contentItem.json && typeof contentItem.json === 'object') {
                  if (contentItem.json.success === false || contentItem.json.error) {
                    status = 'error';
                    break;
                  }
                }
                // Check if text content indicates error
                if (contentItem.text && typeof contentItem.text === 'string') {
                  try {
                    const parsed = JSON.parse(contentItem.text);
                    if (parsed.success === false || parsed.error) {
                      status = 'error';
                      break;
                    }
                  } catch {
                    // Not JSON, ignore
                  }
                }
              }
            }

            toolResultMap.set(toolUseId, {
              content: toolResult.content || [],
              status: status
            });
          }
        }
      }
    }
  }

  // Second pass: match results to tool uses
  return messages.map(message => {
    if (message.role === 'assistant') {
      // Process content blocks to add results to tool uses
      const updatedContent = message.content.map(contentBlock => {
        if ((contentBlock.type === 'toolUse' || contentBlock.type === 'tool_use') && contentBlock.toolUse) {
          const toolUse = contentBlock.toolUse as any;
          const toolUseId = toolUse.toolUseId;

          // Check if we have a result for this tool use
          if (toolUseId && toolResultMap.has(toolUseId)) {
            const result = toolResultMap.get(toolUseId)!;

            // Create updated toolUse with embedded result
            return {
              ...contentBlock,
              toolUse: {
                ...toolUse,
                result: result,
                status: result.status === 'error' ? 'error' : 'complete'
              }
            };
          }
        }
        return contentBlock;
      });

      return {
        ...message,
        content: updatedContent
      };
    }
    return message;
  });
}
