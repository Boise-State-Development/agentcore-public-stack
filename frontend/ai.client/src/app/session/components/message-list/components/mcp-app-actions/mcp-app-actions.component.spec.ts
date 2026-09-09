import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { McpAppActionsComponent } from './mcp-app-actions.component';
import type { McpAppCard } from '../../../../services/mcp-apps/mcp-app-card-state.service';

function card(over: Partial<McpAppCard> = {}): McpAppCard {
  return {
    cardId: `c${Math.random().toString(36).slice(2, 8)}`,
    toolUseId: 'tu1',
    toolName: 'board_snapshot',
    arguments: {},
    content: [{ type: 'text', text: 'ok' }],
    isError: false,
    createdAt: '2026-01-01T00:00:00Z',
    ...over,
  };
}

describe('McpAppActionsComponent', () => {
  let fixture: ComponentFixture<McpAppActionsComponent>;

  function render(cards: McpAppCard[]): string {
    fixture.componentRef.setInput('cards', cards);
    fixture.detectChanges();
    return (fixture.nativeElement.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [McpAppActionsComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(McpAppActionsComponent);
  });

  it('collapses repeated successful calls into one summary line', () => {
    const text = render([
      card({ toolName: 'board_snapshot' }),
      card({ toolName: 'update_task' }),
      card({ toolName: 'board_snapshot' }),
      card({ toolName: 'board_snapshot' }),
    ]);
    expect(text).toContain('4 succeeded');
    expect(text).toContain('board_snapshot ×3');
    expect(text).toContain('update_task');
    // A repeated tool must not produce a row apiece — that's the wall of
    // history this component exists to replace.
    expect(text.match(/board_snapshot/g)).toHaveLength(1);
  });

  it('drops the ×N suffix for a single call', () => {
    expect(render([card({ toolName: 'update_task' })])).toContain(
      '1 succeeded update_task',
    );
  });

  it('lists each failure separately, with its error text', () => {
    const text = render([
      card({ toolName: 'board_snapshot' }),
      card({ toolName: 'update_task', isError: true, content: [{ type: 'text', text: 'task not found' }] }),
      card({ toolName: 'delete_task', isError: true, content: [{ type: 'text', text: 'permission denied' }] }),
    ]);
    expect(text).toContain('1 succeeded');
    // One row per failure, each carrying its own tool name and message.
    // (Asserted without whitespace between the name and the "failed" label —
    // they're adjacent inline spans separated by a margin, not by text.)
    expect(text.match(/failed/g)).toHaveLength(2);
    expect(text).toContain('update_task');
    expect(text).toContain('task not found');
    expect(text).toContain('delete_task');
    expect(text).toContain('permission denied');
  });

  it('omits the success line when everything failed', () => {
    const text = render([card({ isError: true })]);
    expect(text).not.toContain('succeeded');
    expect(text).toContain('failed');
  });

  it('truncates a long error result', () => {
    const text = render([
      card({ isError: true, content: [{ type: 'text', text: 'x'.repeat(400) }] }),
    ]);
    expect(text).toContain('…');
    expect(text).not.toContain('x'.repeat(250));
  });

  it('renders nothing for an empty card list', () => {
    expect(render([])).toBe('');
  });
});
