// Files by reference in the handoff UI (handoffs spec §4.9, PR-0.4a): the review sheet's file
// checkboxes going out, and the handoff row's list of the target's files coming back. The services
// are stubbed; their HTTP and state are covered in agent-handoff.service.spec.ts and
// delivery.service.spec.ts.
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { DeliveryService } from '../../services/handoff/delivery.service';
import { HandoffNotificationService } from '../../services/handoff/handoff-notification.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { HandoffTargetFilesService } from '../../services/handoff/handoff-target-files.service';
import { DeliveryFile, HandoffReview, HandoffView } from '../../services/handoff/handoff.models';
import { SessionService } from '../../services/session/session.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { HandoffReviewSheetComponent } from './handoff-review-sheet.component';
import { HandoffRowComponent } from './handoff-row.component';

function text(el: Element): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('review sheet files', () => {
  const handoff = {
    setFileShared: vi.fn(),
    previewPrompt: () => '',
    setRequest: vi.fn(),
    cancel: vi.fn(),
    send: vi.fn(),
  };

  beforeEach(() => {
    handoff.setFileShared.mockClear();
    TestBed.configureTestingModule({ providers: [{ provide: AgentHandoffService, useValue: handoff }] });
  });

  function render(review: Partial<HandoffReview>): HTMLElement {
    TestBed.inject(HandoffStateService).setReview('src', {
      draft: { agentId: 'rubric', agentName: 'Rubric Builder', request: 'draft it' },
      phase: 'ready',
      request: 'draft it',
      card: 'ctx',
      cardRemoved: false,
      fileIds: [],
      ...review,
    });
    const fixture = TestBed.createComponent(HandoffReviewSheetComponent);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const files = Array.from({ length: 6 }, (_, i) => ({
    uploadId: `f${i}`,
    filename: `file ${i}.pdf`,
    mimeType: 'application/pdf',
    sizeBytes: 1536,
  }));

  it('lists each source file as a labelled checkbox, ticked as the review says', () => {
    const el = render({ files: files.slice(0, 2), fileIds: ['f1'] });

    const legend = el.querySelector('fieldset legend');
    expect(legend && text(legend)).toBe('Files from this conversation');
    const boxes = Array.from(el.querySelectorAll<HTMLInputElement>('fieldset input[type="checkbox"]'));
    expect(boxes.map((b) => b.checked)).toEqual([false, true]);
    expect(text(boxes[0].closest('label')!)).toContain('file 0.pdf');
    expect(text(boxes[0].closest('label')!)).toContain('2 KB');
  });

  it('ticking a file shares it, and a sixth box is disabled once five are ticked', () => {
    const el = render({ files, fileIds: ['f0', 'f1', 'f2', 'f3', 'f4'] });
    const boxes = Array.from(el.querySelectorAll<HTMLInputElement>('fieldset input[type="checkbox"]'));

    expect(boxes[5].disabled).toBe(true);
    expect(boxes[0].disabled).toBe(false);
    boxes[0].click();
    expect(handoff.setFileShared).toHaveBeenCalledWith('src', 'f0', false);
  });

  it('shows no file section when the source has none', () => {
    expect(render({ files: [] }).querySelector('fieldset')).toBeNull();
  });
});

describe('handoff row files', () => {
  const targetFiles = signal<readonly DeliveryFile[]>([]);
  const refresh = vi.fn().mockResolvedValue(undefined);
  const delivery = {
    bringBack: vi.fn(),
    bringBackFile: vi.fn(),
    isBringingBack: () => false,
    wasBroughtBack: () => false,
    isBringingBackFile: () => false,
    wasFileBroughtBack: (_h: string, id: string) => id === 'brought',
  };

  const ROW: HandoffView = {
    handoffId: 'h1',
    sessionId: 'src',
    kind: 'mention',
    status: 'done',
    agentId: 'rubric',
    agentName: 'Rubric Builder',
    request: 'draft a rubric',
    sourceMessageIndex: 1,
    excerptTokens: 10,
    createdAt: '2026-11-12T00:00:00Z',
    updatedAt: '2026-11-12T00:00:00Z',
    targetSessionId: 'tgt',
  };

  beforeEach(() => {
    refresh.mockClear();
    delivery.bringBackFile.mockClear();
    targetFiles.set([]);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AgentHandoffService, useValue: { targetLink: () => ({ commands: ['/s', 'tgt'], queryParams: {} }) } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false } },
        { provide: ToolInsightService, useValue: { latestSummary: () => undefined, status: () => undefined } },
        { provide: SessionService, useValue: { mergedSessionsResource: () => ({ sessions: [] }) } },
        { provide: HandoffNotificationService, useValue: { canOffer: () => false, dismissOffer: vi.fn() } },
        { provide: UserConnectorsService, useValue: { ensureLoaded: vi.fn(), displayNameFor: (id: string) => id } },
        { provide: DeliveryService, useValue: delivery },
        { provide: HandoffTargetFilesService, useValue: { refresh, filesFor: () => targetFiles() } },
      ],
    });
  });

  function render(row: HandoffView = ROW): { el: HTMLElement; detect: () => void } {
    const fixture = TestBed.createComponent(HandoffRowComponent);
    fixture.componentRef.setInput('row', row);
    fixture.detectChanges();
    return { el: fixture.nativeElement as HTMLElement, detect: () => fixture.detectChanges() };
  }

  const file = (uploadId: string, filename: string): DeliveryFile => ({
    uploadId,
    sessionId: 'tgt',
    filename,
    mimeType: 'application/pdf',
    sizeBytes: 10,
  });

  it('reads the target’s files once the row settles, and lists each with Bring back outside the link', () => {
    const { el, detect } = render();
    expect(refresh).toHaveBeenCalledWith('tgt');

    targetFiles.set([file('f1', 'rubric.docx'), file('brought', 'notes.md')]);
    detect();

    const list = el.querySelector('[aria-label="Files Rubric Builder made"]')!;
    expect(list).not.toBeNull();
    expect(list.closest('a')).toBeNull();
    expect(text(list)).toContain('rubric.docx');
    expect(text(list)).toContain('Added to your next message');
    el.querySelector<HTMLButtonElement>('button[aria-label="Bring back rubric.docx"]')!.click();
    expect(delivery.bringBackFile).toHaveBeenCalledWith(ROW, targetFiles()[0]);
  });

  it('a send-back row lists no files and reads none', () => {
    targetFiles.set([file('f1', 'rubric.docx')]);
    const { el } = render({ ...ROW, kind: 'send_back' });

    expect(refresh).not.toHaveBeenCalled();
    expect(el.querySelector('[aria-label="Files Rubric Builder made"]')).toBeNull();
  });
});
