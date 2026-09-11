import { describe, it, expect } from 'vitest';
import {
  describeToolCall,
  describeToolGroup,
  resultCardinality,
  splitToolName,
} from './tool-summary';
import type { ToolCallDisplay } from './tool-rail.model';

function call(overrides: Partial<ToolCallDisplay> = {}): ToolCallDisplay {
  return {
    id: 't1',
    toolName: 'list_assignments',
    input: {},
    status: 'complete',
    ...overrides,
  };
}

function withJson(json: unknown, overrides: Partial<ToolCallDisplay> = {}) {
  return call({ result: { status: 'success', content: [{ json }] }, ...overrides });
}

describe('splitToolName', () => {
  it('splits snake_case into verb and subject', () => {
    expect(splitToolName('list_assignments')).toEqual({
      verb: 'list',
      subject: 'assignments',
    });
  });

  it('splits camelCase', () => {
    expect(splitToolName('getAssignmentDetails')).toEqual({
      verb: 'get',
      subject: 'assignment details',
    });
  });

  it('strips the scoped-tool-id prefix', () => {
    // `toolId::name` is the per-tool MCP enablement routing id. A user must
    // never see it in a sentence.
    expect(splitToolName('canvas-mcp::list_courses')).toEqual({
      verb: 'list',
      subject: 'courses',
    });
  });

  it('treats a bare noun as a subject with no verb', () => {
    expect(splitToolName('calculator')).toEqual({ verb: '', subject: 'calculator' });
  });

  it('does not strip a verb that is the whole name', () => {
    // "search" alone has no subject to describe; treating it as a verb would
    // render "Searched " with a dangling space.
    expect(splitToolName('search')).toEqual({ verb: '', subject: 'search' });
  });

  it('survives an empty name', () => {
    expect(splitToolName('')).toEqual({ verb: '', subject: '' });
  });
});

describe('resultCardinality', () => {
  it('counts a top-level array', () => {
    expect(resultCardinality(withJson([1, 2, 3]))).toBe(3);
  });

  it('counts the single array property of an envelope object', () => {
    expect(resultCardinality(withJson({ courses: ['a', 'b'] }))).toBe(2);
  });

  it('parses JSON delivered as text', () => {
    const c = call({
      result: { status: 'success', content: [{ text: '[{"id":1},{"id":2}]' }] },
    });
    expect(resultCardinality(c)).toBe(2);
  });

  it('refuses to guess when an object has two array properties', () => {
    // Picking one would be arbitrary, and a confidently wrong count reads as
    // a bug. No number is the honest answer.
    expect(
      resultCardinality(withJson({ courses: ['a'], errors: ['x', 'y'] })),
    ).toBeUndefined();
  });

  it('refuses to guess on prose', () => {
    const c = call({
      result: { status: 'success', content: [{ text: 'Everything worked' }] },
    });
    expect(resultCardinality(c)).toBeUndefined();
  });

  it('is undefined with no result yet', () => {
    expect(resultCardinality(call({ status: 'pending' }))).toBeUndefined();
  });
});

describe('describeToolCall', () => {
  it('reports a count for list-shaped calls', () => {
    expect(describeToolCall(withJson({ assignments: ['a', 'b', 'c', 'd'] }))).toBe(
      'Listed 4 assignments',
    );
  });

  it('singularizes a count of one', () => {
    expect(describeToolCall(withJson({ assignments: ['a'] }))).toBe(
      'Listed 1 assignment',
    );
  });

  it('does not attach a count to a get-shaped call', () => {
    // "Read 3 assignment details" would imply three things were read.
    expect(
      describeToolCall(
        withJson({ rubrics: ['a', 'b', 'c'] }, { toolName: 'get_assignment_details' }),
      ),
    ).toBe('Read assignment details');
  });

  it('uses the present progressive while a call is still running', () => {
    expect(describeToolCall(call({ status: 'pending' }))).toBe('Listing assignments');
  });

  it('capitalizes the running form', () => {
    // Regression: the progressive was built from the lowercase verb token, so
    // a running call rendered "listing assignments" mid-sentence-cased.
    expect(describeToolCall(call({ status: 'pending' }))).toMatch(/^[A-Z]/);
  });

  it('doubles the final consonant where English does', () => {
    expect(describeToolCall(call({ toolName: 'get_course', status: 'pending' }))).toBe(
      'Getting course',
    );
    expect(describeToolCall(call({ toolName: 'run_report', status: 'pending' }))).toBe(
      'Running report',
    );
  });

  it('drops the trailing e before -ing', () => {
    expect(
      describeToolCall(call({ toolName: 'create_rubric', status: 'pending' })),
    ).toBe('Creating rubric');
  });

  it('says plainly when a call failed', () => {
    expect(describeToolCall(call({ status: 'error' }))).toBe("Couldn't list assignments");
  });

  it('treats an error result as a failure even when the status says complete', () => {
    // A tool can fail inside a successful invocation; the line must not claim
    // the work succeeded.
    const c = call({
      status: 'complete',
      result: { status: 'error', content: [{ text: '422' }] },
    });
    expect(describeToolCall(c)).toBe("Couldn't list assignments");
  });

  it('adds an article where the subject needs one', () => {
    expect(
      describeToolCall(call({ toolName: 'search_knowledge_base', status: 'pending' })),
    ).toBe('Searching the knowledge base');
  });

  it('falls back to the tool name when there is no recognizable verb', () => {
    expect(describeToolCall(call({ toolName: 'calculator' }))).toBe('Ran calculator');
  });

  it('names browser-shaped tools properly', () => {
    // Regression: `browse_web` fell through to the no-verb path and rendered
    // "Running browse web", which reads like a machine.
    expect(describeToolCall(call({ toolName: 'browse_web', status: 'pending' }))).toBe(
      'Browsing the web',
    );
    expect(describeToolCall(call({ toolName: 'browse_web' }))).toBe('Browsed the web');
  });
});

describe('describeToolGroup', () => {
  it('describes a single call directly', () => {
    expect(describeToolGroup([withJson({ courses: ['a', 'b'] }, { toolName: 'list_courses' })])).toBe(
      'Listed 2 courses',
    );
  });

  it('names the running call and the progress through the group', () => {
    const calls = [
      call({ id: 'a', toolName: 'list_courses', status: 'complete' }),
      call({ id: 'b', toolName: 'list_assignments', status: 'pending' }),
      call({ id: 'c', toolName: 'get_assignment_details', status: 'pending' }),
    ];
    expect(describeToolGroup(calls)).toBe('Listing assignments (1 of 3 done)');
  });

  it('aggregates a homogeneous group instead of chaining names', () => {
    // The whole point of collapsing is a header that does not grow with the
    // group, so three searches must not render as three names.
    const calls = [
      withJson({ hits: ['a'] }, { id: 'a', toolName: 'search_courses' }),
      withJson({ hits: ['b', 'c'] }, { id: 'b', toolName: 'list_courses' }),
    ];
    expect(describeToolGroup(calls)).toBe('Ran 2 lookups on courses (3 results)');
  });

  it('summarizes a mixed pipeline by its first step', () => {
    const calls = [
      withJson({ courses: ['a', 'b', 'c'] }, { id: 'a', toolName: 'list_courses' }),
      call({ id: 'b', toolName: 'list_assignments' }),
      call({ id: 'c', toolName: 'get_assignment_details' }),
    ];
    expect(describeToolGroup(calls)).toBe('Listed 3 courses, then 2 more steps');
  });

  it('uses the singular for a two-step pipeline', () => {
    const calls = [
      withJson({ courses: ['a'] }, { id: 'a', toolName: 'list_courses' }),
      call({ id: 'b', toolName: 'get_assignment_details' }),
    ];
    expect(describeToolGroup(calls)).toBe('Listed 1 course, then 1 more step');
  });

  it('surfaces failures in a finished group', () => {
    const calls = [
      call({ id: 'a', toolName: 'list_courses', status: 'complete' }),
      call({ id: 'b', toolName: 'create_rubric', status: 'error' }),
    ];
    expect(describeToolGroup(calls)).toContain('1 failed');
  });

  it('is empty for an empty group', () => {
    expect(describeToolGroup([])).toBe('');
  });
});
