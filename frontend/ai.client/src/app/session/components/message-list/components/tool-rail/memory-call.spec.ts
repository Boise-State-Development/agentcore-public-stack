import { describe, it, expect } from 'vitest';
import {
  memoryCallKind,
  memoryRollup,
  memoryWriteDetail,
  memoryWriteLabel,
} from './memory-call';
import { describeToolCall, describeToolGroup } from './tool-summary';
import type { ToolCallDisplay } from './tool-rail.model';

function save(input: Record<string, unknown> = {}, overrides: Partial<ToolCallDisplay> = {}): ToolCallDisplay {
  return {
    id: 'm1',
    toolName: 'memory_save',
    input: {
      scope: 'mine',
      slug: 'status-replies',
      text: '- Reply to status checks with STATUS-OK: and one sentence.',
      description: "Status checks: reply 'STATUS-OK:' plus one sentence",
      ...input,
    },
    status: 'complete',
    ...overrides,
  };
}

function call(toolName: string, overrides: Partial<ToolCallDisplay> = {}): ToolCallDisplay {
  return { id: toolName, toolName, input: {}, status: 'complete', ...overrides };
}

describe('memoryCallKind', () => {
  it('reads a save of a new file as a save', () => {
    expect(memoryCallKind(save())).toBe('save');
  });

  it('reads a save that keeps anchors as an update', () => {
    expect(memoryCallKind(save({ text: '- The pilot starts March 3. <!-- e:abc123 -->' }))).toBe('update');
  });

  it('reads a save with replaces as a rename', () => {
    expect(memoryCallKind(save({ replaces: 'old-name' }))).toBe('rename');
  });

  it('reads a save to MEMORY.md as an index update', () => {
    expect(memoryCallKind(save({ slug: 'MEMORY.md' }))).toBe('index');
  });

  it('names the other memory tools', () => {
    expect(memoryCallKind(call('memory_propose'))).toBe('propose');
    expect(memoryCallKind(call('memory_read'))).toBe('read');
    expect(memoryCallKind(call('memory_list'))).toBe('list');
    expect(memoryCallKind(call('memory_query'))).toBe('query');
  });

  it('ignores a scope prefix on the tool name', () => {
    expect(memoryCallKind(call('server::memory_read'))).toBe('read');
  });

  it('returns null for any other tool', () => {
    expect(memoryCallKind(call('list_assignments'))).toBeNull();
    expect(memoryCallKind(call('memory'))).toBeNull();
  });
});

describe('describeToolCall for memory tools', () => {
  it('says a finished save saved a memory, not "Ran memory save"', () => {
    expect(describeToolCall(save())).toBe('Saved a memory');
  });

  it('is present progressive while the save runs', () => {
    expect(describeToolCall(save({}, { status: 'pending' }))).toBe('Saving a memory');
  });

  it('says a failed save failed', () => {
    expect(describeToolCall(save({}, { status: 'error' }))).toBe("Couldn't save a memory");
    expect(
      describeToolCall(save({}, { result: { status: 'error', content: [{ text: 'Not saved' }] } })),
    ).toBe("Couldn't save a memory");
  });

  it('distinguishes updates, renames, the index and proposals', () => {
    expect(describeToolCall(save({ text: '- x <!-- e:1 -->' }))).toBe('Updated a memory');
    expect(describeToolCall(save({ replaces: 'old' }))).toBe('Renamed a memory');
    expect(describeToolCall(save({ slug: 'MEMORY.md' }))).toBe('Updated the memory index');
    expect(describeToolCall(call('memory_propose'))).toBe('Proposed a memory change');
  });

  it('describes reads and listings', () => {
    expect(describeToolCall(call('memory_read'))).toBe('Read a memory');
    expect(describeToolCall(call('memory_list'))).toBe('Checked memories');
    expect(describeToolCall(call('memory_query', { status: 'pending' }))).toBe('Searching memories');
  });
});

describe('describeToolGroup for memory tools', () => {
  it('rolls up a read and two saves', () => {
    const calls = [call('memory_read'), save(), save({}, { id: 'm2' })];
    expect(describeToolGroup(calls)).toBe('Read a memory, saved 2 memories');
  });

  it('counts the failures beside the rollup', () => {
    const calls = [save(), save({}, { id: 'm2', status: 'error' })];
    expect(describeToolGroup(calls)).toBe('Saved a memory, 1 failed');
  });

  it('says so when every memory step failed', () => {
    const calls = [save({}, { status: 'error' }), save({}, { id: 'm2', status: 'error' })];
    expect(describeToolGroup(calls)).toBe("Couldn't complete 2 memory steps");
  });

  it('leaves a mixed group to the general rule', () => {
    const calls = [save(), call('list_assignments')];
    expect(describeToolGroup(calls)).toBe('Saved a memory, then 1 more step');
  });

  it('reports progress while a memory step runs', () => {
    const calls = [call('memory_read'), save({}, { status: 'pending' })];
    expect(describeToolGroup(calls)).toBe('Saving a memory (1 of 2 done)');
  });
});

describe('memoryRollup and memoryWriteLabel', () => {
  it('orders fragments the way a turn does them', () => {
    expect(memoryRollup(['save', 'list', 'read'])).toBe('Checked memories, read a memory, saved a memory');
  });

  it('counts updates and renames together', () => {
    expect(memoryRollup(['update', 'rename'])).toBe('Updated 2 memories');
  });

  it('labels only writes, in lower case', () => {
    expect(memoryWriteLabel(['read', 'save'])).toBe('saved a memory');
    expect(memoryWriteLabel(['propose', 'propose'])).toBe('proposed 2 memory changes');
  });

  it('has no label without a write', () => {
    expect(memoryWriteLabel(['read', 'list'])).toBeNull();
    expect(memoryWriteLabel([])).toBeNull();
  });
});

describe('memoryWriteDetail', () => {
  it('gives the file, its index line and its items without markers or anchors', () => {
    const detail = memoryWriteDetail(
      save({ text: '- The pilot starts March 3. <!-- e:abc -->\n\n- Vendor is Acme.\n' }),
    );
    expect(detail).toEqual({
      scope: 'mine',
      slug: 'status-replies',
      description: "Status checks: reply 'STATUS-OK:' plus one sentence",
      items: ['The pilot starts March 3.', 'Vendor is Acme.'],
    });
  });

  it('places a proposal in the project scope', () => {
    const detail = memoryWriteDetail(
      call('memory_propose', { input: { slug: 'vendor', text: '- Vendor is Acme.' } }),
    );
    expect(detail?.scope).toBe('project');
  });

  it('is null for reads and for a write with no items', () => {
    expect(memoryWriteDetail(call('memory_read', { input: { scope: 'mine', slug: 'x' } }))).toBeNull();
    expect(memoryWriteDetail(save({ text: '' }))).toBeNull();
    expect(memoryWriteDetail(save({ text: 42 }))).toBeNull();
  });
});
