import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import type { Message } from '../../services/models/message.model';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import type { HandoffView } from '../../services/handoff/handoff.models';
import { HandoffNotificationService } from '../../services/handoff/handoff-notification.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import type { WorkflowRunView } from '../../services/handoff/workflow-run.models';

/**
 * Handoffs in the message list (handoffs spec §4.3).
 *
 * 1. A handoff's target streams in the background, so its prompts must never render in the source
 *    thread's own prompt slot. OAuth was the one interrupt service the list didn't scope by session.
 * 2. A handoff row renders after the message that was last when it was sent, or at the tail when
 *    that message isn't in the loaded page.
 */
describe('MessageListComponent — handoffs', () => {
  let fixture: ComponentFixture<MessageListComponent>;

  function message(sessionId: string, index: number): Message {
    return {
      id: `msg-${sessionId}-${index}`,
      role: 'user',
      content: [{ type: 'text', text: `message ${index}` }],
      createdAt: '2026-01-01T00:00:00Z',
    } as unknown as Message;
  }

  function handoff(overrides: Partial<HandoffView> = {}): HandoffView {
    return {
      handoffId: 'h1',
      sessionId: 'src',
      kind: 'mention',
      status: 'done',
      agentId: 'rubric',
      agentName: 'Rubric Builder',
      request: 'draft a rubric for this',
      sourceMessageIndex: 1,
      excerptTokens: 40,
      createdAt: '2026-01-01T00:00:00Z',
      updatedAt: '2026-01-01T00:00:00Z',
      targetSessionId: 'tgt',
      statusLine: 'Drafted a four-row rubric',
      ...overrides,
    };
  }

  function api() {
    return fixture.componentInstance as unknown as {
      unanchoredInterrupts: () => { providerId: string }[];
      unanchoredHandoffRows: () => HandoffView[];
      handoffRowsForMessageId: (id: string) => readonly HandoffView[];
      runRowsForMessageId: (id: string) => readonly WorkflowRunView[];
      unanchoredRunRows: () => WorkflowRunView[];
      leadingRunRows: () => WorkflowRunView[];
    };
  }

  function render(messages: Message[], sessionId: string | null): void {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', messages);
    fixture.componentRef.setInput('sessionId', sessionId);
    fixture.detectChanges();
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

  afterEach(() => {
    fixture?.destroy();
    vi.restoreAllMocks();
  });

  describe('OAuth prompts are scoped to their own conversation', () => {
    it("never renders a background target's consent prompt in the source thread", () => {
      const consent = TestBed.inject(OAuthConsentService);
      consent.requestConsent('canvas', 'https://auth.example/consent', 'i-1', 'msg-tgt-3', 'tgt');

      render([message('src', 0), message('src', 1)], 'src');

      expect(api().unanchoredInterrupts()).toEqual([]);
    });

    it("still renders this thread's own unanchored prompt", () => {
      const consent = TestBed.inject(OAuthConsentService);
      consent.requestConsent('canvas', 'https://auth.example/consent', 'i-1', 'msg-src-9', 'src');

      render([message('src', 0)], 'src');

      expect(api().unanchoredInterrupts().map((r) => r.providerId)).toEqual(['canvas']);
    });

    it('keeps the old behaviour in a list bound to no session', () => {
      const consent = TestBed.inject(OAuthConsentService);
      consent.requestConsent('canvas', 'https://auth.example/consent', 'i-1', 'msg-x-3', 'x');

      render([message('preview', 0)], null);

      expect(api().unanchoredInterrupts().map((r) => r.providerId)).toEqual(['canvas']);
    });
  });

  describe('handoff rows', () => {
    it('anchors a row after the message that was last when it was sent', () => {
      TestBed.inject(HandoffStateService).upsertRow(handoff({ sourceMessageIndex: 1 }));

      render([message('src', 0), message('src', 1), message('src', 2)], 'src');

      expect(api().handoffRowsForMessageId('msg-src-1').map((r) => r.handoffId)).toEqual(['h1']);
      expect(api().handoffRowsForMessageId('msg-src-0')).toEqual([]);
      expect(api().unanchoredHandoffRows()).toEqual([]);
      const link = fixture.nativeElement.querySelector('app-handoff-row a') as HTMLAnchorElement;
      expect(link.getAttribute('href')).toContain('/s/tgt');
      expect(link.getAttribute('aria-label')).toContain('Rubric Builder');
      expect(link.textContent).toContain('Finished · Drafted a four-row rubric');
    });

    it('a row that needs input offers browser notifications beside the link, never inside it', () => {
      const notifications = TestBed.inject(HandoffNotificationService);
      Object.assign(notifications, { canOffer: () => true });
      const requestPermission = vi.spyOn(notifications, 'requestPermission').mockResolvedValue('granted');
      TestBed.inject(HandoffStateService).upsertRow(handoff({ status: 'needs_input', waitingOn: { kind: 'oauth_consent', name: 'canvas' } }));

      render([message('src', 0), message('src', 1)], 'src');

      const row = fixture.nativeElement.querySelector('app-handoff-row') as HTMLElement;
      const buttons = [...row.querySelectorAll('button')] as HTMLButtonElement[];
      expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Get a browser notification next time', 'No thanks']);
      expect(row.querySelector('a button')).toBeNull();
      expect(requestPermission).not.toHaveBeenCalled();
      buttons[0].click();
      expect(requestPermission).toHaveBeenCalledTimes(1);
    });

    it('a finished row makes no notification offer; its one button is Bring back result', () => {
      Object.assign(TestBed.inject(HandoffNotificationService), { canOffer: () => true });
      TestBed.inject(HandoffStateService).upsertRow(handoff({ status: 'done' }));

      render([message('src', 0), message('src', 1)], 'src');

      const buttons = Array.from(fixture.nativeElement.querySelectorAll('app-handoff-row button') as NodeListOf<HTMLElement>);
      expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Bring back result']);
    });

    it("renders a row whose message isn't loaded at the tail rather than dropping it", () => {
      TestBed.inject(HandoffStateService).upsertRow(handoff({ sourceMessageIndex: 40 }));

      render([message('src', 0)], 'src');

      expect(api().unanchoredHandoffRows().map((r) => r.handoffId)).toEqual(['h1']);
    });

    it("never shows another conversation's rows", () => {
      TestBed.inject(HandoffStateService).upsertRow(handoff({ sessionId: 'other' }));

      render([message('src', 0), message('src', 1)], 'src');

      expect(api().handoffRowsForMessageId('msg-src-1')).toEqual([]);
      expect(fixture.nativeElement.querySelector('app-handoff-row')).toBeNull();
    });
  });

  describe('workflow run rows (workflow-runs spec §20.2)', () => {
    function step(i: number, status: HandoffView['status']): HandoffView {
      return handoff({ handoffId: `s${i}`, agentName: `Agent ${i}`, status, runId: 'run1', stepId: `st${i}`, targetSessionId: `t${i}` });
    }

    function run(overrides: Partial<WorkflowRunView> = {}): WorkflowRunView {
      return {
        runId: 'run1',
        sessionId: 'src',
        status: 'approved',
        request: 'r',
        steps: [1, 2].map((i) => ({ stepId: `st${i}`, handoffId: `s${i}`, agentId: 'a', agentName: `Agent ${i}`, request: 'r', removed: false, handoff: step(i, 'done') })),
        fileIds: [],
        limits: { maxSteps: 8, concurrency: 4 },
        sourceMessageIndex: 1,
        createdAt: '',
        updatedAt: '',
        ...overrides,
      };
    }

    it('anchors one run row at its message, and its steps are lines in it, never rows of their own', () => {
      TestBed.inject(HandoffStateService).upsertRow(step(1, 'done'));
      TestBed.inject(HandoffStateService).upsertRow(step(2, 'running'));
      TestBed.inject(HandoffStateService).upsertRow(handoff({ handoffId: 'single' }));
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run()]);

      render([message('src', 0), message('src', 1), message('src', 2)], 'src');

      expect(api().runRowsForMessageId('msg-src-1').map((r) => r.runId)).toEqual(['run1']);
      expect(api().handoffRowsForMessageId('msg-src-1').map((r) => r.handoffId)).toEqual(['single']);
      expect(fixture.nativeElement.querySelectorAll('app-workflow-run-row')).toHaveLength(1);
      expect(fixture.nativeElement.querySelectorAll('app-handoff-row')).toHaveLength(1);
    });

    it('shows a run once approved or stopped, never at its sheet or dismissed', () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [
        run({ runId: 'open', status: 'open' }),
        run({ runId: 'gone', status: 'dismissed' }),
        run({ runId: 'stopped', status: 'stopped' }),
      ]);
      render([message('src', 0), message('src', 1)], 'src');
      expect(api().runRowsForMessageId('msg-src-1').map((r) => r.runId)).toEqual(['stopped']);
    });

    it("renders a run whose message isn't loaded at the tail", () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run({ sourceMessageIndex: 40 })]);
      render([message('src', 0)], 'src');
      expect(api().unanchoredRunRows().map((r) => r.runId)).toEqual(['run1']);
    });

    // A saved workflow started from a new conversation (workflow-designer spec §20.3, PR-D.2):
    // the run is approved before the conversation has a turn, so its anchor is -1.
    it('renders a run from a conversation with no turns, with no messages at all', () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run({ sourceMessageIndex: -1 })]);
      render([], 'src');
      expect(api().leadingRunRows().map((r) => r.runId)).toEqual(['run1']);
      expect(api().unanchoredRunRows()).toEqual([]);
      expect(fixture.nativeElement.querySelectorAll('app-workflow-run-row')).toHaveLength(1);
      // The list's empty placeholder would read wrong under a run's row.
      expect(fixture.nativeElement.textContent).not.toContain('No messages yet');
    });

    it('still says "No messages yet" in an empty conversation with no run', () => {
      render([], 'src');
      expect(fixture.nativeElement.textContent).toContain('No messages yet');
    });

    it('keeps that run above the first turn once the conversation has one', () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run({ sourceMessageIndex: -1 })]);
      render([message('src', 0), message('src', 1)], 'src');
      const rows = fixture.nativeElement.querySelectorAll('app-workflow-run-row');
      expect(rows).toHaveLength(1);
      const firstTurn = fixture.nativeElement.querySelector('app-user-message');
      expect(firstTurn).not.toBeNull();
      expect(rows[0].compareDocumentPosition(firstTurn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(api().leadingRunRows().map((r) => r.runId)).toEqual(['run1']);
    });

    it('falls back to the tail while an older page hides the first turn', () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run({ sourceMessageIndex: -1 })]);
      render([message('src', 30), message('src', 31)], 'src');
      expect(api().leadingRunRows()).toEqual([]);
      expect(api().unanchoredRunRows().map((r) => r.runId)).toEqual(['run1']);
    });
  });
});
