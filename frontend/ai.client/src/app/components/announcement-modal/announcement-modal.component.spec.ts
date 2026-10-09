import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { provideMarkdown } from 'ngx-markdown';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import {
  Announcement,
  AnnouncementSurface,
} from '../../services/announcements/announcement.model';
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

  function setup(
    announcement: Announcement,
    sourceSurface?: AnnouncementSurface,
  ) {
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
          useValue: {
            announcement,
            sourceSurface,
          } satisfies AnnouncementModalData,
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

  /** Escape, as the user presses it inside the dialog. */
  function pressEscape(fixture: ReturnType<typeof setup>) {
    el(fixture)
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  }

  /** A press and release on the area outside the panel. */
  function clickOutside(fixture: ReturnType<typeof setup>) {
    const outside = el(fixture).querySelector('[appDialogDismiss]')!;
    outside.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    outside.dispatchEvent(new MouseEvent('click', { bubbles: true }));
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

      pressEscape(fixture);
      expect(close).toHaveBeenCalledTimes(1);

      clickOutside(fixture);
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

      pressEscape(fixture);
      clickOutside(fixture);

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

  describe('ack attribution', () => {
    it('defaults to the `modal` surface — the §D8 interruption', () => {
      setup(makeAnnouncement());
      expect(ack).toHaveBeenCalledWith('a1', 'seen', 'modal');
    });

    it('attributes every ack to the surface the user came from', () => {
      // Opened by clicking the banner text. The ack row's `surface` is the
      // only record of what drove the dismissal, so it must say `banner` —
      // otherwise banner engagement is indistinguishable from an interruption
      // the user never asked for.
      const fixture = setup(makeAnnouncement(), 'banner');
      expect(ack).toHaveBeenCalledWith('a1', 'seen', 'banner');

      ack.mockClear();
      confirmButton(fixture).click();
      expect(ack).toHaveBeenCalledWith('a1', 'dismissed', 'banner');
    });

    it('carries the surface through an `acknowledged` too', () => {
      const fixture = setup(makeAnnouncement({ requires_ack: true }), 'banner');
      ack.mockClear();
      confirmButton(fixture).click();
      expect(ack).toHaveBeenCalledWith('a1', 'acknowledged', 'banner');
    });
  });

  describe('opened through CDK Dialog', () => {
    function open(announcement: Announcement) {
      TestBed.resetTestingModule();
      ack = vi.fn(async () => true);
      TestBed.configureTestingModule({
        providers: [provideMarkdown(), { provide: AnnouncementsService, useValue: { ack } }],
      });
      return openInCdkDialog<AnnouncementModalComponent, AnnouncementModalData>(AnnouncementModalComponent, {
        data: { announcement },
        disableClose: announcement.requires_ack,
      });
    }

    afterEach(() => TestBed.inject(Dialog).closeAll());

    it('names the dialog from the announcement title', async () => {
      const { container } = await open(makeAnnouncement());
      expectNamedDialog(container, { name: 'Acceptable use policy update' });
      expect(container.querySelector('button[aria-label="Close announcement"]')).not.toBeNull();
    });

    it('stays open on Escape when it must be acknowledged, and is still named', async () => {
      const { ref, container } = await open(makeAnnouncement({ requires_ack: true }));
      expectNamedDialog(container, { name: 'Acceptable use policy update' });
      expect(container.querySelector('button[aria-label="Close announcement"]')).toBeNull();

      let closed = false;
      ref.closed.subscribe(() => (closed = true));
      container
        .querySelector('app-dialog-shell')!
        .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      expect(closed).toBe(false);
    });
  });
});
