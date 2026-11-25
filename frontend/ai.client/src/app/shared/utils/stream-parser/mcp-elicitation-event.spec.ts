import { describe, expect, it, vi } from 'vitest';
import { processStreamEvent, validateMcpElicitationRequiredEvent } from './stream-parser-core';

/**
 * `mcp_elicitation_required` (docs/specs/mcp-elicitation.md §4). The one
 * validator decides what renders on the live stream and on reload, where the
 * breadcrumb holds this same payload JSON-encoded.
 */
function event(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'mcp_elicitation_required',
    interruptId: 'v1:middleware_execute_tool:tu-1:abc',
    toolUseId: 'tu-1',
    toolName: 'github_issues___delete_repository',
    serverName: 'GitHub',
    message: 'Type "acme/scratch" to confirm permanent deletion.',
    fields: [
      {
        name: 'repository_name',
        kind: 'text',
        title: 'Repository name',
        description: null,
        required: true,
        default: null,
        format: null,
        minLength: null,
        maxLength: null,
      },
    ],
    ...overrides,
  };
}

describe('validateMcpElicitationRequiredEvent', () => {
  it('accepts the backend payload', () => {
    expect(validateMcpElicitationRequiredEvent(event())).toBe(true);
  });

  it('accepts every field kind the backend emits', () => {
    const fields = [
      { name: 'a', kind: 'text', title: 'A', required: false },
      { name: 'b', kind: 'number', title: 'B', required: false, minimum: 0, maximum: 1 },
      { name: 'c', kind: 'integer', title: 'C', required: true },
      { name: 'd', kind: 'boolean', title: 'D', required: true },
      {
        name: 'e',
        kind: 'choice',
        title: 'E',
        required: false,
        multiSelect: true,
        options: [{ value: 'x', label: 'X' }],
      },
    ];
    expect(validateMcpElicitationRequiredEvent(event({ fields }))).toBe(true);
  });

  it('accepts an empty server name, which the header replaces', () => {
    expect(validateMcpElicitationRequiredEvent(event({ serverName: '' }))).toBe(true);
  });

  it.each<[string, Record<string, unknown>]>([
    ['the wrong type', { type: 'user_question_required' }],
    ['no interrupt id', { interruptId: '' }],
    ['no tool use id', { toolUseId: undefined }],
    ['no message', { message: undefined }],
    ['no fields', { fields: [] }],
    ['fields not a list', { fields: {} }],
    ['a field with no name', { fields: [{ name: '', kind: 'text', title: 'X', required: true }] }],
    ['an unknown kind', { fields: [{ name: 'x', kind: 'file', title: 'X', required: true }] }],
    ['required not a boolean', { fields: [{ name: 'x', kind: 'text', title: 'X', required: 'yes' }] }],
    [
      'a choice with no options',
      { fields: [{ name: 'x', kind: 'choice', title: 'X', required: true, options: [] }] },
    ],
    [
      'an option with no value',
      { fields: [{ name: 'x', kind: 'choice', title: 'X', required: true, options: [{ label: 'X' }] }] },
    ],
  ])('refuses %s', (_label, overrides) => {
    expect(validateMcpElicitationRequiredEvent(event(overrides))).toBe(false);
  });

  it('dispatches a valid event and reports an invalid one', () => {
    const onMcpElicitationRequired = vi.fn();
    const onParseError = vi.fn();

    processStreamEvent('mcp_elicitation_required', event(), { onMcpElicitationRequired, onParseError });
    processStreamEvent('mcp_elicitation_required', event({ fields: [] }), {
      onMcpElicitationRequired,
      onParseError,
    });

    expect(onMcpElicitationRequired).toHaveBeenCalledTimes(1);
    expect(onParseError).toHaveBeenCalledWith('mcp_elicitation_required: invalid data structure');
  });
});
