import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import type { Message } from '../../services/models/message.model';

/**
 * Continue after a max_tokens cut, as a reloaded conversation shows it.
 *
 * On a model that refuses assistant prefill (Claude 4.6 and later), Continue
 * persists a fixed user turn ahead of the continuation, and `GET /messages`
 * marks it `continuation: true`. The user never typed it, so the list must not
 * draw it as their bubble or start a new turn at it: the partial and the
 * continuation are one answer.
 *
 * No `detectChanges`, for the reason the turn-recap spec gives: grouping is a
 * signal, and rendering assistant markdown leaks a KaTeX rejection under
 * `isolate: false`.
 */
describe('MessageListComponent — a continued answer', () => {
  let fixture: ComponentFixture<MessageListComponent>;

  interface Segment {
    kind: 'user' | 'assistant';
    messages: Message[];
  }
  interface Turn {
    segments: Segment[];
  }

  function msg(index: number, role: 'user' | 'assistant', text: string, extra: Partial<Message> = {}): Message {
    return {
      id: `msg-sess1-${index}`,
      role,
      content: [{ type: 'text', text }],
      createdAt: '2026-01-01T00:00:00Z',
      ...extra,
    } as Message;
  }

  function turns(messages: Message[]): Turn[] {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', messages);
    return (fixture.componentInstance as unknown as { turns: () => Turn[] }).turns();
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
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]), provideMarkdown()],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  const PROMPT = 'Continue from exactly where you stopped, without repeating what you already wrote.';

  it('renders the partial and its continuation as one answer in one turn', () => {
    const result = turns([
      msg(0, 'user', 'Fill in the checklist'),
      msg(1, 'assistant', '| Clause | Status |\n| 1 | Met |'),
      msg(2, 'user', PROMPT, { continuation: true }),
      msg(3, 'assistant', '| 2 | Not met |'),
    ]);

    expect(result).toHaveLength(1);
    const [only] = result;
    expect(only.segments.map((s) => s.kind)).toEqual(['user', 'assistant']);
    // The user segment is the real prompt; the continue prompt draws nothing.
    expect(only.segments[0].messages.map((m) => m.id)).toEqual(['msg-sess1-0']);
    expect(only.segments[1].messages.map((m) => m.id)).toEqual(['msg-sess1-1', 'msg-sess1-3']);
  });

  it('still starts a turn at a user message with the same words but no flag', () => {
    // Only the server's flag hides a message, never its text: a person who
    // types the same sentence said it.
    const result = turns([
      msg(0, 'user', 'Fill in the checklist'),
      msg(1, 'assistant', 'partial'),
      msg(2, 'user', PROMPT),
      msg(3, 'assistant', 'rest'),
    ]);

    expect(result).toHaveLength(2);
  });
});
