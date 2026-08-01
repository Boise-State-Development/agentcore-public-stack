import { describe, it, expect } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import {
  ReportAgentDialogComponent,
  ReportAgentDialogData,
  ReportAgentDialogResult,
} from './report-agent-dialog.component';

/**
 * One dialog serves two entry points — the store's detail page and the foot of a
 * conversation — and `sessionId` is the only thing that tells them apart.
 *
 * The case worth pinning hardest is the **unticked** one. Attaching a conversation is the
 * user handing curators a pointer to something they said; unticking it is them taking that
 * back. If the result still carried the id, the backend would keep storing it and the
 * consent would be un-withdrawable from the UI.
 *
 * DI tokens rather than vi.mock, per project convention — a shared worker pool makes
 * module mocks leak across specs.
 */
describe('ReportAgentDialogComponent', () => {
  let closedWith: ReportAgentDialogResult[];

  function build(data: Partial<ReportAgentDialogData> = {}): ReportAgentDialogComponent {
    closedWith = [];
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: DialogRef,
          useValue: { close: (v: ReportAgentDialogResult) => closedWith.push(v) },
        },
        {
          provide: DIALOG_DATA,
          useValue: { agentName: 'Policy Lookup', ...data } satisfies ReportAgentDialogData,
        },
      ],
    });
    return TestBed.createComponent(ReportAgentDialogComponent).componentInstance;
  }

  describe('opened from the store page (no conversation)', () => {
    it('offers nothing to attach', () => {
      expect(build().fromConversation()).toBe(false);
    });

    it('keeps the report framing', () => {
      expect(build().submitLabel()).toBe('Send report');
    });

    it('sends no sessionId', () => {
      const component = build();
      component.reason.set('broken');
      component.onSubmit();

      expect(closedWith[0]?.sessionId).toBeUndefined();
    });
  });

  describe('opened from a conversation', () => {
    it('switches to feedback framing', () => {
      const component = build({ sessionId: 'sess-123' });

      expect(component.fromConversation()).toBe(true);
      expect(component.submitLabel()).toBe('Send feedback');
    });

    it('offers the conversation ticked, because that is what makes it actionable', () => {
      expect(build({ sessionId: 'sess-123' }).attachConversation()).toBe(true);
    });

    it('attaches the conversation when left ticked', () => {
      const component = build({ sessionId: 'sess-123' });
      component.reason.set('suggestion');
      component.onSubmit();

      expect(closedWith[0]).toEqual({
        reason: 'suggestion',
        note: undefined,
        sessionId: 'sess-123',
      });
    });

    it('sends no conversation at all when unticked', () => {
      // Not `sessionId: ''`, not a separate flag — absent. An amendment that still carried
      // the id would make the user's withdrawal un-actionable on the server.
      const component = build({ sessionId: 'sess-123' });
      component.reason.set('broken');
      component.attachConversation.set(false);
      component.onSubmit();

      expect(closedWith[0]?.sessionId).toBeUndefined();
    });

    it('says it is amending when the user already has one open', () => {
      expect(build({ sessionId: 'sess-123', hasOpenReport: true }).submitLabel()).toBe(
        'Update feedback',
      );
    });
  });

  describe('the reason set', () => {
    it('offers suggestion — feedback from a conversation is often not a defect', () => {
      expect(build({ sessionId: 'sess-123' }).reasons.map((r) => r.value)).toContain('suggestion');
    });

    it('will not submit without one, so the queue can always sort by severity', () => {
      const component = build();
      component.onSubmit();

      expect(closedWith).toHaveLength(0);
    });

    it('closes with undefined on cancel, which is how the caller reads "cancelled"', () => {
      build().onCancel();

      expect(closedWith).toEqual([undefined]);
    });
  });
});
