import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import { Announcement } from '../../services/announcements/announcement.model';
import { AnnouncementBannerComponent } from './announcement-banner.component';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills\n\n- one',
    summary: null,
    surfaces: ['panel', 'banner'],
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

describe('AnnouncementBannerComponent', () => {
  let bannerItem: ReturnType<typeof signal<Announcement | null>>;
  let ack: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    bannerItem = signal<Announcement | null>(null);
    ack = vi.fn(async () => true);

    // DI-token override rather than vi.mock, per house convention.
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: { bannerItem, ack } },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function create() {
    const fixture = TestBed.createComponent(AnnouncementBannerComponent);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof create>) {
    return (fixture.nativeElement as HTMLElement).textContent?.trim() ?? '';
  }

  function strip(fixture: ReturnType<typeof create>): HTMLElement | null {
    return (fixture.nativeElement as HTMLElement).querySelector('[role="status"]');
  }

  it('renders nothing when the server sent no banner', () => {
    const fixture = create();
    expect(strip(fixture)).toBeNull();
  });

  it('renders the strip once the feed resolves', () => {
    // The dependency-set regression from #974: read the derived state while
    // the feed is still EMPTY first, then populate it. A computed that
    // guard-clauses out before touching its signal tracks nothing on that
    // first evaluation and never recomputes — which is how the submit button
    // on the admin form shipped permanently disabled.
    const fixture = create();
    const component = fixture.componentInstance;
    expect(component.bannerText()).toBe('');
    expect(strip(fixture)).toBeNull();

    bannerItem.set(makeAnnouncement());
    fixture.detectChanges();

    expect(component.bannerText()).toBe('Skills are here');
    expect(strip(fixture)).not.toBeNull();
    expect(text(fixture)).toContain('Skills are here');
  });

  it('prefers the summary over the title — the strip is one line', () => {
    bannerItem.set(
      makeAnnouncement({ title: 'A'.repeat(140), summary: 'Short version' }),
    );
    const fixture = create();
    expect(text(fixture)).toContain('Short version');
    expect(text(fixture)).not.toContain('A'.repeat(140));
  });

  it('falls back to the title when the summary is blank whitespace', () => {
    bannerItem.set(makeAnnouncement({ summary: '   ' }));
    const fixture = create();
    expect(fixture.componentInstance.bannerText()).toBe('Skills are here');
  });

  it('writes `seen` on render, and only once per announcement', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();

    expect(ack).toHaveBeenCalledWith('a1', 'seen', 'banner');
    expect(ack).toHaveBeenCalledTimes(1);

    fixture.detectChanges();
    fixture.detectChanges();
    expect(ack).toHaveBeenCalledTimes(1);
  });

  it('writes `seen` again when a different announcement takes the slot', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    ack.mockClear();

    bannerItem.set(makeAnnouncement({ announcement_id: 'a2' }));
    fixture.detectChanges();

    expect(ack).toHaveBeenCalledWith('a2', 'seen', 'banner');
  });

  it('records a durable `dismissed` ack on ✕, scoped to the banner surface', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    ack.mockClear();

    const dismiss = (fixture.nativeElement as HTMLElement).querySelector(
      'button',
    ) as HTMLButtonElement;
    expect(dismiss.getAttribute('aria-label')).toBe(
      'Dismiss announcement: Skills are here',
    );
    dismiss.click();

    expect(ack).toHaveBeenCalledWith('a1', 'dismissed', 'banner');
  });

  it('is a polite live region, never assertive', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    const el = strip(fixture)!;
    expect(el.getAttribute('aria-live')).toBe('polite');
    expect(el.getAttribute('role')).toBe('status');
  });

  it.each([
    ['info' as const, 'heroInformationCircle', 'state-info'],
    ['success' as const, 'heroCheckCircle', 'state-success'],
    ['warning' as const, 'heroExclamationTriangle', 'state-warning'],
  ])('maps %s severity to its icon and token scale', (severity, icon, scale) => {
    bannerItem.set(makeAnnouncement({ severity }));
    const fixture = create();
    const component = fixture.componentInstance;

    expect(component.iconName()).toBe(icon);
    // Full literal class strings — a concatenated `bg-state-${severity}-50`
    // would compile to nothing, because Tailwind scans source text.
    expect(component.severityClass()).toContain(`bg-${scale}-50`);
    expect(component.severityClass()).toContain(`dark:bg-${scale}-900/30`);
  });

  it('renders the CTA only when both label and url are present', () => {
    bannerItem.set(makeAnnouncement({ cta_label: 'Read more' }));
    let fixture = create();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('a'),
    ).toBeNull();

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: { bannerItem, ack } },
      ],
    });
    bannerItem.set(
      makeAnnouncement({ cta_label: 'Read more', cta_url: 'https://example.edu' }),
    );
    fixture = create();
    const link = (fixture.nativeElement as HTMLElement).querySelector('a')!;
    expect(link.getAttribute('href')).toBe('https://example.edu');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  describe('overlays rather than occupying space', () => {
    it('positions the host absolutely, so dismissing it cannot reflow the page', () => {
      // The regression: the banner used to be a flex child of the shell's
      // <main>, so showing or hiding it pulled the whole view up or down by
      // its height. It must never participate in flow.
      bannerItem.set(makeAnnouncement());
      const fixture = create();
      const host = fixture.nativeElement as HTMLElement;

      expect(host.className).toContain('absolute');
      expect(host.className).not.toContain('block');
    });

    it('lets clicks through the positioning strip to whatever is beneath', () => {
      // The strip spans the composer's full width. Without this, it would
      // swallow clicks aimed at the message list behind it.
      bannerItem.set(makeAnnouncement());
      const fixture = create();
      const host = fixture.nativeElement as HTMLElement;

      expect(host.className).toContain('pointer-events-none');
      expect(strip(fixture)!.className).toContain('pointer-events-auto');
    });

    it('floats above the composer rather than stacking in flow with it', () => {
      // `bottom-full` against the `relative` chat-input host: the pill sits
      // clear of the quota tabs, which stay attached to the input, and
      // dismissing it cannot move the composer under the user's cursor.
      bannerItem.set(makeAnnouncement());
      const fixture = create();
      const host = (fixture.nativeElement as HTMLElement).className;
      expect(host).toContain('bottom-full');
      expect(host).not.toContain('top-16');
    });

    it('takes the other side when the composer is centred', () => {
      // The empty state puts the greeting immediately above a centred
      // composer, so `above` would float the pill over it — visibly so at
      // narrow widths, where the greeting wraps.
      bannerItem.set(makeAnnouncement());
      const fixture = TestBed.createComponent(AnnouncementBannerComponent);
      fixture.componentRef.setInput('placement', 'below');
      fixture.detectChanges();

      const host = (fixture.nativeElement as HTMLElement).className;
      expect(host).toContain('top-full');
      expect(host).not.toContain('bottom-full');
    });

    it('reads as a compact tab beside the quota warning, not a full-bleed strip', () => {
      bannerItem.set(makeAnnouncement());
      const fixture = create();
      const pill = strip(fixture)!;

      expect(pill.className).toContain('rounded-2xl');
      expect(pill.className).toContain('shadow-md');
      // Shrink-to-fit, so it reads as a sibling of the quota tab rather than
      // a bar spanning the composer.
      expect(pill.className).toContain('inline-flex');
      expect(pill.className).toContain('text-xs');
      // The old full-bleed look leaned on a bottom border instead.
      expect(pill.className).not.toContain('border-b');
    });

    it('sets no layout variable on the document — nothing offsets against it', () => {
      bannerItem.set(makeAnnouncement());
      create();
      expect(
        document.documentElement.style.getPropertyValue(
          '--announcement-banner-height',
        ),
      ).toBe('');
    });
  });
});
