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
import { joinDeliveryBlocks, renderDeliveryBlock } from '../../services/handoff/handoff-prompt';
import { DeliveryView } from '../../services/handoff/handoff.models';
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
