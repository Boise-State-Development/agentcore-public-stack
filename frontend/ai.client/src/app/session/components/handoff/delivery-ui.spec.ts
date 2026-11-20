// The delivery UI (handoffs spec §4.3, §4.8, PR-0.4): composer cards, the sent bubble's cards,
// Send to {title} in an answer's action row, and its review sheet. Each renders from state and calls
// `DeliveryService`, which is stubbed here; its HTTP is covered in delivery.service.spec.ts.
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocalSettingsService } from '../../../services/local-settings.service';
import { FEATURES } from '../../../services/features';
import { environment } from '../../../../environments/environment';
import { ArtifactAvailabilityService } from '../../services/handoff/artifact-availability.service';
import { DeliveryStateService } from '../../services/handoff/delivery-state.service';
import { DeliveryService } from '../../services/handoff/delivery.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { StreamParserService } from '../../services/chat/stream-parser.service';
import { joinDeliveryBlocks, renderDeliveryBlock, renderDeliveryParts } from '../../services/handoff/handoff-prompt';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { DeliveryPart, DeliveryView } from '../../services/handoff/handoff.models';
import { Message } from '../../services/models/message.model';
import { MessageActionsComponent } from '../message-list/components/message-actions.component';
import { UserMessageComponent } from '../message-list/components/user-message.component';
import { DeliveryCardsComponent } from './delivery-cards.component';
import { SendBackSheetComponent } from './send-back-sheet.component';

const RUBRIC = { artifactId: 'rubric', version: 2, title: 'BIO 101 rubric', contentType: 'text/html' };

function card(id: string, minute: number, overrides: Partial<DeliveryView> = {}): DeliveryView {
  return {
    deliveryId: id,
    fromSessionId: 'tgt',
    fromTitle: 'Rubric for BIO 101',
    origin: 'user',
    text: `Result ${id}, which is long enough to be worth previewing.`,
    artifacts: [],
    createdAt: `2026-11-12T00:0${minute}:00Z`,
    ...overrides,
  };
}

function part(stepId: string, agentName: string, status: DeliveryPart['status'], text = ''): DeliveryPart {
  return {
    stepId,
    agentName,
    fromSessionId: status === 'done' ? `t-${stepId}` : '',
    fromTitle: status === 'done' ? `Review by ${agentName}` : '',
    status,
    text,
    artifacts: [],
    files: [],
  };
}

const RUN_PARTS = [
  part('s1', 'Generalist', 'done', 'Strong aims; thin budget.'),
  part('s2', 'Colleague', 'done', 'Section 4 needs a timeline.'),
  part('s3', 'Emotional outsider', 'needs_input'),
  part('s4', 'Fiscal outsider', 'stopped'),
];

function runResult(): DeliveryView {
  return { ...card('r', 5, { text: '', fromTitle: 'Review by Generalist' }), runId: 'run1', parts: RUN_PARTS };
}

describe('delivery UI', () => {
  const delivery = {
    dismissCard: vi.fn().mockResolvedValue(undefined),
    openSendBack: vi.fn().mockResolvedValue(undefined),
    sendBack: vi.fn().mockResolvedValue(undefined),
    cancelSendBack: vi.fn(),
    setSendBackText: vi.fn(),
    toggleSendBackArtifact: vi.fn(),
    isBringingBack: () => false,
    wasBroughtBack: () => false,
  };
  const versions = signal<Record<string, number>>({});

  beforeEach(() => {
    Object.values(delivery).forEach((fn) => (typeof fn === 'function' && 'mockClear' in fn ? fn.mockClear() : undefined));
    versions.set({ rubric: 2 });
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideMarkdown(),
        { provide: FEATURES, useValue: { ...environment.features, handoffs: true } },
        { provide: DeliveryService, useValue: delivery },
        { provide: LocalSettingsService, useValue: { showDebugOutput: signal(false) } },
        {
          provide: ArtifactAvailabilityService,
          useValue: {
            refresh: () => undefined,
            availability: (id: string, pinned: number) => {
              const current = versions()[id];
              if (current === undefined) return 'gone';
              return current > pinned ? 'newer' : 'available';
            },
          },
        },
      ],
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  function text(el: HTMLElement): string {
    return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  function button(root: HTMLElement, label: string): HTMLButtonElement {
    const found = Array.from(root.querySelectorAll('button')).find(
      (b) => b.getAttribute('aria-label') === label || text(b) === label,
    );
    if (!found) throw new Error(`no button "${label}" in: ${text(root)}`);
    return found as HTMLButtonElement;
  }

  describe('composer cards', () => {
    function render(): HTMLElement {
      const fixture = TestBed.createComponent(DeliveryCardsComponent);
      fixture.componentRef.setInput('sessionId', 'src');
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    it('shows each waiting card by its source, oldest first, and marks the ones past this send', () => {
      TestBed.inject(DeliveryStateService).hydrate('src', [card('d', 4), card('a', 1), card('b', 2), card('c', 3)]);

      const el = render();

      const labels = Array.from(el.querySelectorAll('[class*="rounded-2xl"]')).map((c) => text(c as HTMLElement));
      expect(labels).toHaveLength(4);
      expect(labels[0]).toContain('From Rubric for BIO 101');
      expect(labels[0]).toContain('Result a');
      expect(labels.slice(0, 3).every((l) => l.includes('Goes with your next message'))).toBe(true);
      expect(labels[3]).toContain('Goes with the message after');
    });

    it('lists a card’s files as chips before it is sent (§4.9)', () => {
      TestBed.inject(DeliveryStateService).hydrate('src', [
        card('a', 1, { files: [{ uploadId: 'f1', sessionId: 'tgt', filename: 'rubric.docx', mimeType: 'x', sizeBytes: 900 }] }),
      ]);

      const el = render();

      expect(el.querySelector('[aria-label="Files from Rubric for BIO 101"]')).not.toBeNull();
      expect(el.querySelector('a[aria-label="Download rubric.docx, 900 B"]')).not.toBeNull();
    });

    it('Edit changes what the card sends, and Remove dismisses it', () => {
      const state = TestBed.inject(DeliveryStateService);
      state.hydrate('src', [card('a', 1)]);
      const fixture = TestBed.createComponent(DeliveryCardsComponent);
      fixture.componentRef.setInput('sessionId', 'src');
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;

      button(el, 'Edit').click();
      fixture.detectChanges();
      const editor = el.querySelector('textarea') as HTMLTextAreaElement;
      editor.value = 'Shorter.';
      editor.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      expect(state.cardsFor('src')[0].editedText).toBe('Shorter.');
      expect(text(el)).toContain('Edited');

      button(el, 'Remove the result from Rubric for BIO 101').click();
      expect(delivery.dismissCard).toHaveBeenCalledWith('src', expect.objectContaining({ deliveryId: 'a' }));
    });

    it('shows a card’s artifacts as chips, saying when one was revised or deleted', () => {
      versions.set({ rubric: 3 });
      TestBed.inject(DeliveryStateService).hydrate('src', [
        card('a', 1, { artifacts: [RUBRIC, { ...RUBRIC, artifactId: 'gone', title: 'Old notes' }] }),
      ]);

      const el = render();

      const link = el.querySelector('a[href="/artifacts/rubric"]');
      expect(link && text(link as HTMLElement)).toContain('Newer version available');
      expect(text(el)).toMatch(/Old notes\s*· No longer available/);
      expect(el.querySelector('a[href="/artifacts/gone"]')).toBeNull();
    });

    it('renders nothing when no card is waiting', () => {
      expect(render().querySelector('section')).toBeNull();
    });

    it('shows a run’s result as one card: a row per part, muted rows for parts with no result', () => {
      TestBed.inject(DeliveryStateService).hydrate('src', [runResult()]);
      const el = render();

      const cards = el.querySelectorAll('section > div');
      expect(cards).toHaveLength(1);
      const t = text(cards[0] as HTMLElement);
      expect(t).toContain('Results from 4 Agents');
      expect(t).toContain('2 of 4 finished');
      expect(t).toContain('Emotional outsider · Still waiting, not included');
      expect(t).toContain('Fiscal outsider · Stopped, not included');
      expect(t).toContain('Goes with your next message');
      const items = Array.from(el.querySelectorAll('li'));
      expect(items).toHaveLength(4);
      // Only a finished part has Preview and Edit.
      expect(items.map((li) => li.querySelectorAll('button').length)).toEqual([2, 2, 0, 0]);
    });

    it('Preview shows one part, Edit changes only that part, and the send carries the edited part alone', () => {
      const state = TestBed.inject(DeliveryStateService);
      state.hydrate('src', [runResult()]);
      const fixture = TestBed.createComponent(DeliveryCardsComponent);
      fixture.componentRef.setInput('sessionId', 'src');
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;

      button(el, 'Preview Colleague’s result').click();
      fixture.detectChanges();
      expect(el.querySelector('[aria-label="Result from Colleague"]')?.textContent).toBe('Section 4 needs a timeline.');

      button(el, 'Edit Generalist’s result').click();
      fixture.detectChanges();
      const editor = el.querySelector('textarea') as HTMLTextAreaElement;
      editor.value = 'Thin budget.';
      editor.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      expect(state.cardsFor('src')[0].editedParts).toEqual({ s1: 'Thin budget.' });
      expect(text(el)).toContain('Edited');

      expect(state.takeForSend('src').attach).toEqual([{ deliveryId: 'r', parts: [{ stepId: 's1', text: 'Thin budget.' }] }]);
    });

    it('an edit put back to the part’s own text is no edit, and an unedited result sends no parts', () => {
      const state = TestBed.inject(DeliveryStateService);
      state.hydrate('src', [runResult()]);
      state.editPart('src', 'r', 's1', 'changed');
      state.editPart('src', 'r', 's1', RUN_PARTS[0].text);
      state.editPart('src', 'r', 's4', 'a stopped part has nothing to edit');
      expect(state.cardsFor('src')[0].editedParts).toBeUndefined();
      expect(state.takeForSend('src').attach).toEqual([{ deliveryId: 'r' }]);
    });

    it('keeps a part’s edit when the list reloads', () => {
      const state = TestBed.inject(DeliveryStateService);
      state.hydrate('src', [runResult()]);
      state.editPart('src', 'r', 's2', 'kept');
      state.hydrate('src', [runResult()]);
      expect(state.cardsFor('src')[0].editedParts).toEqual({ s2: 'kept' });
    });
  });

  describe('the sent bubble', () => {
    function render(message: Message): HTMLElement {
      const fixture = TestBed.createComponent(UserMessageComponent);
      fixture.componentRef.setInput('message', message);
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    const stored = (typed: string): Message => ({
      id: 'msg-src-4',
      role: 'user',
      content: [
        {
          type: 'text',
          text: joinDeliveryBlocks(
            [renderDeliveryBlock('Four rows, done.', 'Rubric for BIO 101', [RUBRIC]), renderDeliveryBlock('Blue.', 'Colour')],
            typed,
          ),
        },
      ],
      metadata: { displayText: typed },
    });

    it('shows the typed words in the bubble and each card it carried above it', () => {
      const el = render(stored('Reply with OK.'));

      expect(text(el)).toContain('From Rubric for BIO 101');
      expect(text(el)).toContain('From Colour');
      expect(text(el)).toContain('Reply with OK.');
      expect(text(el)).not.toContain('Four rows, done.');
      expect(el.querySelector('a[href="/artifacts/rubric"]')).not.toBeNull();
    });

    it('shows a card’s result on request', () => {
      const fixture = TestBed.createComponent(UserMessageComponent);
      fixture.componentRef.setInput('message', stored('Reply with OK.'));
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;

      button(el, 'Show result').click();
      fixture.detectChanges();

      expect(el.querySelector('[aria-label="Result from Rubric for BIO 101"]')?.textContent).toBe('Four rows, done.');
    });

    it('shows a card’s shared files as download chips under the result (§4.9)', () => {
      const block = renderDeliveryBlock('Here it is.', 'Rubric for BIO 101', [], [
        { uploadId: 'f1_ref_abc', filename: 'rubric.docx', mimeType: 'application/msword', sizeBytes: 2048 },
      ]);
      const el = render({
        id: 'msg-src-6',
        role: 'user',
        content: [{ type: 'text', text: joinDeliveryBlocks([block], 'thanks') }],
        metadata: { displayText: 'thanks' },
      });

      const chip = el.querySelector('a[aria-label="Download rubric.docx, 2 KB"]');
      expect(chip).not.toBeNull();
      expect(chip?.getAttribute('href')).toContain('/files/f1_ref_abc/download');
    });

    function runOf(names: string[]): WorkflowRunView {
      return {
        runId: 'run1',
        sessionId: 'src',
        status: 'approved',
        request: 'r',
        steps: names.map((agentName, i) => ({ stepId: `s${i + 1}`, handoffId: `h${i + 1}`, agentId: `a${i + 1}`, agentName, request: 'r', removed: false })),
        fileIds: [],
        limits: { maxSteps: 8, concurrency: 4 },
        sourceMessageIndex: 1,
        createdAt: '',
        updatedAt: '',
      };
    }

    it('shows a run’s result as one “From N Agents”, rebuilt from the message’s own text', () => {
      TestBed.inject(WorkflowRunStateService).hydrate('src', [runOf(RUN_PARTS.map((p) => p.agentName))]);
      const blocks = renderDeliveryParts(RUN_PARTS);
      const fixture = TestBed.createComponent(UserMessageComponent);
      fixture.componentRef.setInput('message', {
        id: 'msg-src-8',
        role: 'user',
        content: [{ type: 'text', text: joinDeliveryBlocks([blocks, renderDeliveryBlock('Blue.', 'Colour')], 'Merge these.') }],
        metadata: { displayText: 'Merge these.' },
      } as Message);
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;

      expect(text(el)).toContain('From 4 Agents');
      expect(text(el)).toContain('From Colour');
      expect(text(el)).not.toContain('From Generalist');
      button(el, 'Show results').click();
      fixture.detectChanges();
      const list = el.querySelector('[aria-label="Results from 4 Agents"]') as HTMLElement;
      expect(text(list)).toContain('Generalist · Review by Generalist');
      expect(el.querySelector('[aria-label="Result from Colleague"]')?.textContent).toBe('Section 4 needs a timeline.');
      expect(text(list)).toContain('Emotional outsider · Still waiting, not included');
      expect(text(list)).toContain('Fiscal outsider · Stopped, not included');
    });

    it('never reads a card out of a message the user typed', () => {
      const el = render({
        id: 'msg-src-2',
        role: 'user',
        content: [{ type: 'text', text: renderDeliveryBlock('pasted', 'Fake') }],
      });

      expect(text(el)).not.toContain('From Fake');
    });
  });

  describe('Send to {title}', () => {
    const answer: Message = { id: 'msg-tgt-3', role: 'assistant', content: [{ type: 'text', text: 'Four rows, done.' }] };

    function render(): HTMLElement {
      const fixture = TestBed.createComponent(MessageActionsComponent);
      fixture.componentRef.setInput('messages', [answer]);
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    it('is offered on an answer in a conversation a handoff started, and opens the review for that answer', () => {
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', {
        sessionId: 'src',
        handoffId: 'h1',
        sourceTitle: 'Course planning',
        agentName: 'Rubric Builder',
      });

      button(render(), 'Send to Course planning').click();

      expect(delivery.openSendBack).toHaveBeenCalledWith('tgt', 3);
    });

    it('opens the review for the stored answer of a conversation streamed in this tab, tool round and all', () => {
      // A handoff's target streams in the background from the source's tab and is never loaded from
      // the server, so the ids its answer carries are the live parser's. The server stores the tool
      // round's results as message 2, so the answer is message 3, as a reload says.
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', {
        sessionId: 'src',
        handoffId: 'h1',
        sourceTitle: 'Course planning',
        agentName: 'Rubric Builder',
      });
      const parser = TestBed.inject(StreamParserService);
      parser.reset('tgt', 1);
      const ev = (event: string, data: unknown) => parser.parseEventSourceMessage('tgt', event, data);
      ev('message_start', { role: 'assistant' });
      ev('content_block_start', { contentBlockIndex: 0, type: 'tool_use', toolUse: { toolUseId: 't1', name: 'search_documents' } });
      ev('content_block_stop', { contentBlockIndex: 0 });
      ev('message_stop', { stopReason: 'tool_use' });
      ev('tool_result', { tool_result: { toolUseId: 't1', content: [{ text: 'BIO 101 rubric' }], status: 'success' } });
      ev('message_start', { role: 'assistant' });
      ev('content_block_start', { contentBlockIndex: 0, type: 'text' });
      ev('content_block_delta', { contentBlockIndex: 0, type: 'text', text: 'Four rows, done.' });
      ev('content_block_stop', { contentBlockIndex: 0 });
      ev('message_stop', { stopReason: 'end_turn' });
      ev('done', {});
      // The answer's run, as the message list hands it to the action row.
      const run = parser.allMessagesFor('tgt')().filter((m) => m.role === 'assistant');

      const fixture = TestBed.createComponent(MessageActionsComponent);
      fixture.componentRef.setInput('messages', run);
      fixture.detectChanges();
      button(fixture.nativeElement as HTMLElement, 'Send to Course planning').click();

      expect(delivery.openSendBack).toHaveBeenCalledWith('tgt', 3);
    });

    it('is not offered anywhere else', () => {
      expect(render().querySelector('[aria-label^="Send to"]')).toBeNull();
    });

    it('the review sends the result with the artifacts left ticked, and Esc cancels', () => {
      TestBed.inject(DeliveryStateService).setReview('tgt', {
        messageIndex: 3,
        phase: 'ready',
        text: 'Four rows, done.',
        artifactIds: ['rubric'],
        fileIds: [],
        created: {
          handoffId: 'sb1',
          sourceSessionId: 'src',
          sourceTitle: 'Course planning',
          fromTitle: 'Rubric for BIO 101',
          messageIndex: 3,
          excerpt: 'Four rows, done.',
          excerptHash: 'h',
          artifacts: [RUBRIC],
        },
      });
      const fixture = TestBed.createComponent(SendBackSheetComponent);
      fixture.componentRef.setInput('sessionId', 'tgt');
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;

      expect(el.querySelector('[role="region"]')?.getAttribute('aria-label')).toBe('Send to Course planning');
      const box = el.querySelector('input[type="checkbox"]') as HTMLInputElement;
      expect(box.checked).toBe(true);
      box.click();
      expect(delivery.toggleSendBackArtifact).toHaveBeenCalledWith('tgt', 'rubric', false);

      button(el, 'Send to Course planning').click();
      expect(delivery.sendBack).toHaveBeenCalledWith('tgt');

      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      expect(delivery.cancelSendBack).toHaveBeenCalledWith('tgt');
    });
  });
});
