import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { StreamParserService } from '../../services/chat/stream-parser.service';
import type { AgentStatusEvent } from '../../../shared/utils/stream-parser';

/**
 * What a long, quiet turn says.
 *
 * Silence on an open stream is usually a hard task, not a broken one: a long
 * tool call or a model thinking without a streamed summary sends nothing for
 * as long as it runs. So silence earns a muted hint under the status, and the
 * amber warning is kept for silence nothing explains — no `agent_status` for
 * the turn at all. A turn in a known phase that truly hangs is ended by the
 * server's own read timeout, with an explicit error.
 */
describe('MessageListComponent — long-running turn feedback', () => {
  let fixture: ComponentFixture<MessageListComponent>;
  let insights: ToolInsightService;
  let parser: StreamParserService;

  const SESSION = 'sess-stall-hint';

  interface LoaderSignals {
    loaderNotice: () => string | null;
    stallHint: () => string | null;
    loaderStatus: () => string | null;
  }

  const view = () => fixture.componentInstance as unknown as LoaderSignals;

  function status(
    phase: AgentStatusEvent['phase'],
    extra: Partial<AgentStatusEvent> = {},
  ): AgentStatusEvent {
    return { type: 'agent_status', sessionId: SESSION, phase, ...extra } as AgentStatusEvent;
  }

  function render(): void {
    // Opens the stream's parser state, which stamps `lastEventAt` now.
    parser.reset(SESSION);
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', []);
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.componentRef.setInput('sessionId', SESSION);
    fixture.detectChanges();
  }

  function silence(ms: number): void {
    vi.advanceTimersByTime(ms);
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
    parser = TestBed.inject(StreamParserService);
    TestBed.inject(ChatStateService).setViewedSession(SESSION);
  });

  afterEach(() => {
    parser.clearSession(SESSION);
    insights.clearStatus(SESSION);
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('says nothing extra on a turn that is still talking', () => {
    render();
    silence(10_000);
    expect(view().stallHint()).toBeNull();
    expect(view().loaderNotice()).toBeNull();
  });

  it('keeps a long tool call on its own label, with a quiet hint and no warning', () => {
    render();
    insights.recordStatus(
      SESSION,
      status('tool_start', { toolUseId: 't1', toolName: 'document_extract' }),
    );
    silence(35_000);

    expect(view().loaderStatus()).toBe('Running');
    expect(view().stallHint()).toBe('Large tasks can take a few minutes.');
    expect(view().loaderNotice()).toBeNull();
  });

  it('never turns a long model call amber, however quiet it gets', () => {
    // The server owns the hang case here: the read timeout ends a silent
    // model call with an explicit error.
    render();
    insights.recordStatus(SESSION, status('thinking', { cycle: 1 }));
    silence(4 * 60_000);

    expect(view().loaderNotice()).toBeNull();
    expect(view().loaderStatus()).toBe('Waiting for the model');
    expect(view().stallHint()).toBe(
      'Large tasks can take several minutes. You can stop it at any time.',
    );
  });

  it('warns in amber only when nothing has said what the turn is doing', () => {
    render();
    silence(35_000);
    expect(view().stallHint()).toBe('Large tasks can take a few minutes.');
    expect(view().loaderNotice()).toBeNull();

    silence(60_000);
    expect(view().loaderNotice()).toBe('No response yet — still waiting…');
    // One message at a time: the warning replaces the hint.
    expect(view().stallHint()).toBeNull();
  });

  it('drops the warning once the turn reports a phase', () => {
    render();
    silence(95_000);
    expect(view().loaderNotice()).not.toBeNull();

    insights.recordStatus(SESSION, status('thinking', { cycle: 1 }));
    fixture.detectChanges();
    expect(view().loaderNotice()).toBeNull();
  });

  it('lets a retry outrank the hint', () => {
    render();
    insights.recordStatus(SESSION, status('thinking', { cycle: 1 }));
    silence(35_000);
    parser.parseEventSourceMessage(SESSION, 'model_retry', {
      type: 'model_retry',
      attempt: 1,
      delaySeconds: 2,
    });
    fixture.detectChanges();

    expect(view().loaderNotice()).toBe('The model is busy. Retrying…');
    expect(view().stallHint()).toBeNull();
  });
});
