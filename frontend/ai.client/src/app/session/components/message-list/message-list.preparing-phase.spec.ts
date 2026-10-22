import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import type { AgentStatusEvent } from '../../../shared/utils/stream-parser';

/**
 * The render delay on the `preparing` phase
 * (docs/specs/agent-state-feedback.md PR-3).
 *
 * The backend announces the agent build unconditionally, because it CANNOT
 * time it: `create_agent` is synchronous, so a cold build occupies the
 * runtime's event loop for its whole duration and a server-side timer never
 * fires. That was verified the expensive way — a 1548ms build on dev, six
 * times the 250ms threshold, emitted nothing at all.
 *
 * So the decision lives here, where the clock is not blocked. What matters:
 * a slow build reaches the screen, a fast one never does, and the phase can
 * never strand the label after the turn has moved on.
 */
describe('MessageListComponent — preparing phase', () => {
  let fixture: ComponentFixture<MessageListComponent>;
  let insights: ToolInsightService;
  let chatState: ChatStateService;

  const SESSION = 'sess-preparing';

  function status(phase: AgentStatusEvent['phase'], cycle?: number): AgentStatusEvent {
    return {
      type: 'agent_status',
      sessionId: SESSION,
      phase,
      ...(cycle === undefined ? {} : { cycle }),
    } as AgentStatusEvent;
  }

  /** The label the loading indicator would show right now. */
  function label(): string | null {
    return (
      fixture.componentInstance as unknown as { loaderStatus: () => string | null }
    ).loaderStatus();
  }

  function render(): void {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', []);
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.componentRef.setInput('sessionId', SESSION);
    fixture.detectChanges();
  }

  beforeEach(() => {
    vi.useFakeTimers();
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
    insights = TestBed.inject(ToolInsightService);
    chatState = TestBed.inject(ChatStateService);
    chatState.setViewedSession(SESSION);
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('does not show a build that finishes quickly', () => {
    // The warm path: 0-40ms measured on dev.
    //
    // The sequence matters more than the timings. An earlier version of this
    // test sent `thinking` 40ms after `preparing` and passed — while
    // production rendered "Getting ready…" on a 1ms build. Production does
    // NOT send `thinking` next: it sends `prepared` when the build ends, and
    // `thinking` only after the head-of-turn work, hundreds of ms later. A
    // test that skips `prepared` is testing a stream the backend never emits.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();

    vi.advanceTimersByTime(1);
    insights.recordStatus(SESSION, status('prepared'));
    fixture.detectChanges();

    // The real gap before the model call — far past the settle delay.
    vi.advanceTimersByTime(1000);
    insights.recordStatus(SESSION, status('thinking', 1));
    fixture.detectChanges();

    expect(label()).not.toBe('Getting ready');
  });

  it('stops showing a slow build the moment it finishes', () => {
    // Cold build: the label earns its place, then must give it up when the
    // wait it describes is over — not when the next unrelated phase happens
    // to arrive.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();
    expect(label()).toBe('Getting ready');

    insights.recordStatus(SESSION, status('prepared'));
    fixture.detectChanges();

    expect(label()).toBe('Thinking');
  });

  it('keeps the label off while prepared waits for the model', () => {
    // The window this bug lived in: build over, model call not yet started.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    vi.advanceTimersByTime(1);
    insights.recordStatus(SESSION, status('prepared'));
    fixture.detectChanges();

    vi.advanceTimersByTime(5000);
    fixture.detectChanges();

    expect(label()).toBe('Thinking');
  });

  it('shows a build that is still running after the delay', () => {
    // The cold path: 1478ms measured.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();

    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(label()).toBe('Getting ready');
  });

  it('reads as Thinking while the delay has not elapsed', () => {
    // Not a blank and not a flicker — the label the user was already seeing.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();

    vi.advanceTimersByTime(100);
    fixture.detectChanges();

    expect(label()).toBe('Thinking');
  });

  it('gives the label up as soon as the model starts', () => {
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();
    expect(label()).toBe('Getting ready');

    insights.recordStatus(SESSION, status('thinking', 1));
    fixture.detectChanges();

    expect(label()).toBe('Waiting for the model');
  });

  it('re-arms for a second build in the same conversation', () => {
    // An @-mention turn builds a second agent, so `preparing` can legitimately
    // come round again. A latch that only opened once would miss it.
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    insights.recordStatus(SESSION, status('thinking', 1));
    fixture.detectChanges();
    expect(label()).toBe('Waiting for the model');

    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    expect(label()).toBe('Thinking');

    vi.advanceTimersByTime(300);
    fixture.detectChanges();
    expect(label()).toBe('Getting ready');
  });

  it('keeps the label steady on the way out of preparing', () => {
    // The regression this fix exists for. Clearing the settle flag when the
    // phase LEFT `preparing` meant the label could compute with the phase
    // still `preparing` and the flag already false, and fall through to
    // "Thinking" — a visible step backwards, observed on dev on every turn.
    //
    // A unit test cannot see that propagation window, so what is pinned here
    // is the invariant that removes it: leaving the phase must not disarm the
    // flag, so a late read while still `preparing` can only ever say
    // "Getting ready".
    render();
    insights.recordStatus(SESSION, status('preparing'));
    fixture.detectChanges();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();
    expect(label()).toBe('Getting ready');

    insights.recordStatus(SESSION, status('thinking', 1));
    fixture.detectChanges();
    expect(label()).toBe('Waiting for the model');

    // The flag survived the exit, so a read that still sees `preparing`
    // resolves to the label the user was already looking at.
    insights.recordStatus(SESSION, status('preparing'));
    expect(label()).toBe('Getting ready');
  });

  it('does not show another conversation’s build', () => {
    render();
    insights.recordStatus('some-other-session', status('preparing'));
    fixture.detectChanges();

    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(label()).toBe('Thinking');
  });
});
