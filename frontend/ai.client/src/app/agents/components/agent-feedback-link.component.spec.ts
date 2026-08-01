import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';

import { AgentFeedbackLinkComponent } from './agent-feedback-link.component';
import { AgentApiService } from '../services/agent-api.service';
import { SubmitReportRequest, SubmitReportResponse } from '../models/store.model';

/**
 * The link is self-contained: dialog → POST → confirmation, with nothing bubbling up.
 * So the things worth testing are what it *sends* and what it *says afterwards*.
 *
 * The confirmation wording is not cosmetic. One open report per reporter (D15.4) means a
 * second send amends the first — telling someone their feedback was "sent" when it
 * amended leaves them expecting two things in a queue that only has one.
 *
 * DI tokens rather than vi.mock, per project convention.
 */
describe('AgentFeedbackLinkComponent', () => {
  let sent: { id: string; request: SubmitReportRequest }[];
  let dialogResult: unknown;
  let response: SubmitReportResponse;
  let failing: boolean;

  function build(sessionId: string | null = 'sess-123'): AgentFeedbackLinkComponent {
    sent = [];
    failing = false;
    response = {
      agentId: 'ast-001',
      reason: 'broken',
      state: 'open',
      createdAt: '2026-07-31T00:00:00Z',
      replacedExisting: false,
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: Dialog,
          useValue: { open: vi.fn(() => ({ closed: of(dialogResult) })) },
        },
        {
          provide: AgentApiService,
          useValue: {
            reportAgent: (id: string, request: SubmitReportRequest) => {
              sent.push({ id, request });
              return failing ? throwError(() => new Error('nope')) : of(response);
            },
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(AgentFeedbackLinkComponent);
    fixture.componentRef.setInput('agentId', 'ast-001');
    fixture.componentRef.setInput('agentName', 'Policy Lookup');
    fixture.componentRef.setInput('sessionId', sessionId);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  beforeEach(() => {
    dialogResult = { reason: 'broken', note: 'It errors out', sessionId: 'sess-123' };
  });

  it('posts what the dialog returned, conversation and all', async () => {
    await build().onOpen();

    expect(sent).toEqual([
      { id: 'ast-001', request: { reason: 'broken', note: 'It errors out', sessionId: 'sess-123' } },
    ]);
  });

  it('sends nothing when the user cancels', async () => {
    dialogResult = undefined;
    await build().onOpen();

    expect(sent).toHaveLength(0);
  });

  it('confirms in place rather than leaving the link looking untouched', async () => {
    const component = build();
    await component.onOpen();

    expect(component['confirmation']()).toMatch(/curate the store/i);
  });

  it('says it updated, not sent, when it amended an open report (D15.4)', async () => {
    const component = build();
    response = { ...response, replacedExisting: true };
    await component.onOpen();

    expect(component['confirmation']()).toMatch(/updated the feedback/i);
  });

  it('tells the dialog the user already has one open, after the first send', async () => {
    const component = build();
    await component.onOpen();
    await component.onOpen();

    const dialog = TestBed.inject(Dialog) as unknown as { open: ReturnType<typeof vi.fn> };
    expect(dialog.open.mock.calls[0][1].data.hasOpenReport).toBe(false);
    expect(dialog.open.mock.calls[1][1].data.hasOpenReport).toBe(true);
  });

  it('surfaces a failure instead of a false confirmation', async () => {
    const component = build();
    failing = true;
    await component.onOpen();

    expect(component['confirmation']()).toBeNull();
    expect(component['error']()).toMatch(/failed/i);
    expect(component['busy']()).toBe(false);
  });

  it('still opens without a conversation — the store page has no session either', async () => {
    dialogResult = { reason: 'suggestion' };
    await build(null).onOpen();

    expect(sent[0].request).toEqual({ reason: 'suggestion' });
  });
});
