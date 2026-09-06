import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { provideMarkdown } from 'ngx-markdown';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import { Announcement } from '../../services/announcements/announcement.model';
import {
  AnnouncementModalComponent,
  AnnouncementModalData,
} from './announcement-modal.component';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Acceptable use policy update',
    body_markdown: '## Policy\n\n- one\n- two',
    summary: null,
    surfaces: ['panel', 'modal'],
    severity: 'info',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-02-01T00:00:00Z',
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    is_unread: true,
    is_updated: false,
    ...overrides,
  };
}

describe('AnnouncementModalComponent', () => {
  let ack: ReturnType<typeof vi.fn>;
  let close: ReturnType<typeof vi.fn>;

  function setup(announcement: Announcement) {
    TestBed.resetTestingModule();
    ack = vi.fn(async () => true);
    close = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        // The template renders <markdown>, so the real MarkdownService is needed.
        provideMarkdown(),
        { provide: AnnouncementsService, useValue: { ack } },
        { provide: DialogRef, useValue: { close, closed: { subscribe: vi.fn() } } },
        {
          provide: DIALOG_DATA,
          useValue: { announcement } satisfies AnnouncementModalData,
        },
      ],
    });
    const fixture = TestBed.createComponent(AnnouncementModalComponent);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  function el(fixture: ReturnType<typeof setup>) {
    return fixture.nativeElement as HTMLElement;
  }

  function confirmButton(fixture: ReturnType<typeof setup>) {
    const buttons = [...el(fixture).querySelectorAll('button')];
    return buttons.find(b => /Got it|I understand/.test(b.textContent ?? ''))!;
  }

  it('records `seen` as soon as it renders', () => {
    setup(makeAnnouncement());
    expect(ack).toHaveBeenCalledWith('a1', 'seen', 'modal');
  });

  it('renders the title and the markdown body', () => {
    const fixture = setup(makeAnnouncement());
    expect(el(fixture).textContent).toContain('Acceptable use policy update');
    const body = el(fixture).querySelector('.message-block');
    expect(body).not.toBeNull();
    // `prose` is inert in this app — the typography plugin is not installed.
    expect(el(fixture).querySelector('.prose')).toBeNull();
  });

  describe('without requiresAck', () => {
    it('labels the confirm button "Got it" and records `dismissed`', () => {
      const fixture = setup(makeAnnouncement());
      ack.mockClear();

      const button = confirmButton(fixture);
      expect(button.textContent?.trim()).toBe('Got it');
      button.click();

      expect(ack).toHaveBeenCalledWith('a1', 'dismissed', 'modal');
      expect(close).toHaveBeenCalled();
    });

    it('offers a ✕, and it records `dismissed` too', () => {
      const fixture = setup(makeAnnouncement());
      ack.mockClear();

      const dismiss = el(fixture).querySelector(
        'button[aria-label="Close announcement"]',
      ) as HTMLButtonElement;
      expect(dismiss).not.toBeNull();
      dismiss.click();

      expect(ack).toHaveBeenCalledWith('a1', 'dismissed', 'modal');
      expect(close).toHaveBeenCalled();
    });

    it('closes on Escape and on a backdrop click', () => {
      const fixture = setup(makeAnnouncement());
      const component = fixture.componentInstance as unknown as {
        onEscape(): void;
        onBackdropDismiss(): void;
      };

      component.onEscape();
      expect(close).toHaveBeenCalledTimes(1);

      component.onBackdropDismiss();
      expect(close).toHaveBeenCalledTimes(2);
    });
  });

  describe('with requiresAck', () => {
    it('labels the confirm button "I understand" and records `acknowledged`', () => {
      const fixture = setup(makeAnnouncement({ requires_ack: true }));
      ack.mockClear();

      const button = confirmButton(fixture);
      expect(button.textContent?.trim()).toBe('I understand');
      button.click();

      expect(ack).toHaveBeenCalledWith('a1', 'acknowledged', 'modal');
      expect(close).toHaveBeenCalled();
    });

    it('has no ✕ — the button is the only exit', () => {
      const fixture = setup(makeAnnouncement({ requires_ack: true }));
      expect(
        el(fixture).querySelector('button[aria-label="Close announcement"]'),
      ).toBeNull();
    });

    it('ignores Escape and backdrop clicks, writing no ack', () => {
      const fixture = setup(makeAnnouncement({ requires_ack: true }));
      ack.mockClear();
      const component = fixture.componentInstance as unknown as {
        onEscape(): void;
        onBackdropDismiss(): void;
      };

      component.onEscape();
      component.onBackdropDismiss();

      expect(close).not.toHaveBeenCalled();
      expect(ack).not.toHaveBeenCalled();
    });
  });

  it('renders the CTA only when both label and url are present', () => {
    let fixture = setup(makeAnnouncement({ cta_label: 'Read the policy' }));
    expect(el(fixture).querySelector('a')).toBeNull();

    fixture = setup(
      makeAnnouncement({
        cta_label: 'Read the policy',
        cta_url: 'https://example.edu/policy',
      }),
    );
    const link = el(fixture).querySelector('a')!;
    expect(link.getAttribute('href')).toBe('https://example.edu/policy');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('is a labelled modal dialog', () => {
    const fixture = setup(makeAnnouncement());
    const panel = el(fixture).querySelector('[role="dialog"]')!;
    expect(panel.getAttribute('aria-modal')).toBe('true');
    const labelledBy = panel.getAttribute('aria-labelledby')!;
    // Attribute selector, not `#id`: the id is a UUID that can start with a
    // digit, and jsdom has no `CSS.escape` to fix that up.
    expect(
      el(fixture).querySelector(`[id="${labelledBy}"]`)?.textContent,
    ).toContain('Acceptable use policy update');
  });
});
