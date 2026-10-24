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
});
