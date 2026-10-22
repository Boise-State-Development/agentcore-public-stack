import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import type { Message } from '../../services/models/message.model';

/**
 * The end-of-turn recap (docs/specs/agent-state-feedback.md).
 *
 * When a turn ends, everything that described it disappears: the loading line
 * goes and takes the elapsed timer with it. The rail keeps per-tool durations,
 * but nothing said how long the turn took.
 *
 * The number is `turnDurationMs`, NOT `latency.endToEndLatency` — the
 * persisted form of that field prefers the provider's own API-call time, so
 * summing it across a turn drops tool execution and the agent build. A turn
 * the user watched for 9s would read as 3s. That distinction is the whole
 * reason a new field exists, so it is what these tests pin.
 */
describe('MessageListComponent — end-of-turn recap', () => {
  let fixture: ComponentFixture<MessageListComponent>;

  function assistant(
    index: number,
    opts: { turnDurationMs?: number; tools?: number; endToEnd?: number } = {},
  ): Message {
    const metadata: Record<string, unknown> = {};
    if (opts.turnDurationMs !== undefined) {
      metadata['turnDurationMs'] = opts.turnDurationMs;
    }
    if (opts.endToEnd !== undefined) {
      metadata['latency'] = { endToEndLatency: opts.endToEnd };
    }
    return {
      id: `msg-sess1-${index}`,
      role: 'assistant',
      content: [
        { type: 'text', text: 'done' },
        ...Array.from({ length: opts.tools ?? 0 }, (_, i) => ({
          type: 'toolUse',
          toolUse: { toolUseId: `t${index}-${i}`, name: 'list_courses', input: {} },
        })),
      ],
      createdAt: '2026-01-01T00:00:00Z',
      metadata: Object.keys(metadata).length ? metadata : undefined,
    } as unknown as Message;
  }

  function user(index: number): Message {
    return {
      id: `msg-sess1-${index}`,
      role: 'user',
      content: [{ type: 'text', text: 'hi' }],
      createdAt: '2026-01-01T00:00:00Z',
    } as unknown as Message;
  }

  /**
   * Deliberately NO `detectChanges`. The recap is derived from signals, so it
   * needs no render — and rendering an assistant message pulls ngx-markdown
   * into a KaTeX dependency whose async render rejects after teardown and,
   * under `isolate: false`, leaks into unrelated spec files. Same hazard the
   * shared-artifacts spec sidesteps by using user-role messages, which this
   * one cannot: a recap needs an assistant run to describe.
   */
  function recap(
    messages: Message[],
    streamingId: string | null = null,
    loading = false,
  ): string | null {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', messages);
    fixture.componentRef.setInput('streamingMessageId', streamingId);
    fixture.componentRef.setInput('isChatLoading', loading);
    const api = fixture.componentInstance as unknown as {
      lastTurnRecap: () => string | null;
    };
    return api.lastTurnRecap();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    TestBed.configureTestingModule({
      imports: [MessageListComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideMarkdown(),
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('reports the duration of a finished turn', () => {
    expect(recap([user(0), assistant(1, { turnDurationMs: 9600 })])).toBe('9.6s');
  });

  it('counts the tools the turn ran', () => {
    expect(
      recap([user(0), assistant(1, { turnDurationMs: 9600, tools: 4 })]),
    ).toBe('9.6s · 4 tools');
  });

  it('counts tools across every message of the run', () => {
    // The agent loop starts a new message per tool round trip, so a turn's
    // tools are spread over several messages.
    const messages = [
      user(0),
      assistant(1, { tools: 2 }),
      assistant(2, { turnDurationMs: 12000, tools: 1 }),
    ];
    expect(recap(messages)).toBe('12s · 3 tools');
  });

  it('says tool, singular, for one', () => {
    expect(
      recap([user(0), assistant(1, { turnDurationMs: 4000, tools: 1 })]),
    ).toBe('4s · 1 tool');
  });

  it('shows nothing while the turn is still streaming', () => {
    const messages = [user(0), assistant(1, { turnDurationMs: 9600 })];
    expect(recap(messages, 'msg-sess1-1')).toBeNull();
  });

  it('shows nothing for a turn from before the field existed', () => {
    // Absent rather than zero: a conversation loaded from history has no
    // turn duration, and inventing one would be a number nobody measured.
    expect(recap([user(0), assistant(1, {})])).toBeNull();
  });

  it('ignores endToEndLatency, which is a different number', () => {
    // The trap this field exists to avoid: `endToEndLatency` persists as the
    // provider's API-call time, so using it here would under-report every
    // turn that ran a tool.
    expect(recap([user(0), assistant(1, { endToEnd: 3000, tools: 2 })])).toBeNull();
  });

  it('formats a long turn in minutes and seconds', () => {
    expect(recap([user(0), assistant(1, { turnDurationMs: 72000 })])).toBe('1m 12s');
  });

  /**
   * The recap is the loading line's `@else`, sharing one slot with it. These
   * two pin the halves of that contract that a rendering test would not: the
   * recap belongs to the LATEST turn only, and it never coexists with the
   * loader.
   */
  it('describes the latest turn, not the one before it', () => {
    // Two complete turns. The first one's 9.6s is not a fact the user asked
    // for any more — only the turn that just finished gets a mark.
    const messages = [
      user(0),
      assistant(1, { turnDurationMs: 9600, tools: 4 }),
      user(2),
      assistant(3, { turnDurationMs: 4000 }),
    ];
    expect(recap(messages)).toBe('4s');
  });

  it('shows nothing while the turn is still loading', () => {
    // `isChatLoading` spans the whole turn and clears at stream close, where
    // `streamingMessageId` clears earlier at message_stop. Gating on the
    // narrower signal alone put the recap on screen underneath a loader that
    // was still running — so this asserts the wider one, with the narrower
    // one deliberately already cleared.
    const messages = [user(0), assistant(1, { turnDurationMs: 9600 })];
    expect(recap(messages, null, true)).toBeNull();
  });
});
