import { describe, expect, it } from 'vitest';
import { matchToolResultsToToolUses } from './tool-results';
import type { Message } from '../models/message.model';

/**
 * A reloaded conversation must agree with what streamed: an MCP App form left
 * open for the user keeps its `awaiting_user_submission` marker, so the frame
 * shows "Waiting for you" after a refresh too, not "Failed".
 */
describe('matchToolResultsToToolUses', () => {
  function conversation(toolResult: Record<string, unknown>): Message[] {
    return [
      {
        id: 'a1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 't1', name: 'issue_write', input: {} } }],
      },
      {
        id: 'u1',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 't1', ...toolResult } }],
      },
    ] as unknown as Message[];
  }

  const resultOf = (messages: Message[]) =>
    messages[0].content[0].toolUse?.result as Record<string, unknown> | undefined;

  it('keeps structuredContent on the merged result', () => {
    const merged = matchToolResultsToToolUses(
      conversation({
        status: 'error',
        isError: true,
        content: [{ text: 'An interactive form has been shown to the user' }],
        structuredContent: { status: 'awaiting_user_submission' },
      }),
    );

    expect(resultOf(merged)).toEqual({
      status: 'error',
      content: [{ text: 'An interactive form has been shown to the user' }],
      structuredContent: { status: 'awaiting_user_submission' },
    });
  });

  it('adds no structuredContent key when the result had none', () => {
    const merged = matchToolResultsToToolUses(
      conversation({ status: 'success', content: [{ text: 'ok' }] }),
    );

    expect(resultOf(merged)).toEqual({ status: 'success', content: [{ text: 'ok' }] });
  });

  it("keeps the tool's _meta (Strands' metadata) for an MCP App to render from", () => {
    const widgetContext = { 'atlassian.com/jira-widget-context': { cloudId: 'c-1' } };
    const merged = matchToolResultsToToolUses(
      conversation({
        status: 'success',
        content: [{ text: '{"issues": []}' }],
        structuredContent: { issues: { nodes: [] } },
        metadata: widgetContext,
      }),
    );

    expect(resultOf(merged)?.['metadata']).toEqual(widgetContext);
  });

  it('adds no metadata key for an empty or non-object metadata', () => {
    for (const metadata of [{}, null, 'x', ['a']]) {
      const merged = matchToolResultsToToolUses(
        conversation({ status: 'success', content: [{ text: 'ok' }], metadata }),
      );

      expect(resultOf(merged)).not.toHaveProperty('metadata');
    }
  });
  it('keeps what the user did with an MCP server form, and nothing else from it', () => {
    const merged = matchToolResultsToToolUses(
      conversation({
        status: 'error',
        content: [{ text: 'Repository deletion was not confirmed' }],
        elicitation: { action: 'decline', extra: 'dropped' },
      }),
    );

    expect(resultOf(merged)?.['elicitation']).toEqual({ action: 'decline' });
  });

  it('drops an outcome it cannot read', () => {
    const merged = matchToolResultsToToolUses(
      conversation({ status: 'error', content: [], elicitation: { action: 'shrug' } }),
    );

    expect(resultOf(merged)).not.toHaveProperty('elicitation');
  });
});
